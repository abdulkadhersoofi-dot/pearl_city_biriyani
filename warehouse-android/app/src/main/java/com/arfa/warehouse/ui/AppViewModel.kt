package com.arfa.warehouse.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.arfa.warehouse.WarehouseApp
import com.arfa.warehouse.data.AuthNeeded
import com.arfa.warehouse.data.CachedOrder
import com.arfa.warehouse.data.CountInfo
import com.arfa.warehouse.data.OrderCard
import com.arfa.warehouse.data.OrderDetail
import com.arfa.warehouse.data.Pending
import com.arfa.warehouse.data.Receipt
import com.arfa.warehouse.data.Reply
import com.arfa.warehouse.scan.Matcher
import com.arfa.warehouse.sync.QueueRules
import com.arfa.warehouse.sync.SyncWorker
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.IOException

const val RACK_PREFIX = "RACK:"

enum class HomeTab { PIPELINE, MINE, DELIVER, DONE, RECEIVE }

sealed class Screen {
    object Loading : Screen()
    object SignIn : Screen()
    object ChoosePin : Screen()
    object Pin : Screen()
    data class Home(val tab: HomeTab) : Screen()
    data class Order(val id: Long) : Screen()
    data class Scan(val target: ScanTarget) : Screen()
    data class ReceiptView(val id: Long) : Screen()
    data class CountView(val id: Long) : Screen()
    data class Deliver(val id: Long) : Screen()
    object Settings : Screen()
}

sealed class ScanTarget {
    data class Pick(val orderId: Long, val lineId: Long) : ScanTarget()
    data class Unit(val receiptId: Long, val lineId: Long) : ScanTarget()
    data class Count(val countId: Long) : ScanTarget()
    /** The delivery check: each unit leaving must be an invoiced serial. */
    data class Deliver(val orderId: Long) : ScanTarget()
    /** Locate: scan box after box; beeps on the serial being looked for. */
    data class Locate(val orderId: Long) : ScanTarget()
    /** Move: scan a rack label, then the units put on it. */
    object Move : ScanTarget()
}

/** What the scanner shows after a read: green (ok) or red, and why. When
 * the box is the right item but its serial isn't in ARFA, offerAdd holds
 * the serial the picker may add and pick. */
data class Feedback(val ok: Boolean, val message: String, val offerAdd: String? = null)

class AppViewModel(application: Application) : AndroidViewModel(application) {

    private val app = application as WarehouseApp
    private val session = app.session
    private val api = app.api
    private val db = app.db

    private val _screen = MutableStateFlow<Screen>(Screen.Loading)
    val screen: StateFlow<Screen> = _screen.asStateFlow()
    val busy = MutableStateFlow(false)
    val message = MutableStateFlow<String?>(null)
    val orders = MutableStateFlow<List<OrderCard>>(emptyList())
    val order = MutableStateFlow<OrderDetail?>(null)
    val orderOffline = MutableStateFlow(false)
    val receipts = MutableStateFlow<List<Receipt>>(emptyList())
    val receipt = MutableStateFlow<Receipt?>(null)
    val counts = MutableStateFlow<List<CountInfo>>(emptyList())
    val count = MutableStateFlow<CountInfo?>(null)
    val update = MutableStateFlow<Pair<String, String>?>(null)     // (version name, apk link)
    private val back = ArrayDeque<Screen>()

    init {
        _screen.value = when {
            !session.signedIn -> Screen.SignIn
            !session.pinSet -> Screen.ChoosePin
            else -> Screen.Pin
        }
        viewModelScope.launch {
            app.events.collect {
                when (_screen.value) {
                    is Screen.Home -> refresh()
                    is Screen.Order, is Screen.Deliver -> reloadOrder()
                    else -> Unit
                }
            }
        }
    }

    // --- moving around ---------------------------------------------------------------------------

    fun go(next: Screen) {
        val current = _screen.value
        if (current != next) back.addLast(current)
        _screen.value = next
        when (next) {
            is Screen.Home -> refresh()
            is Screen.Order -> openOrder(next.id)
            is Screen.Deliver -> openOrder(next.id)
            is Screen.ReceiptView -> openReceipt(next.id)
            else -> Unit
        }
    }

    /** Back: true when handled inside the app. */
    fun back(): Boolean {
        val current = _screen.value
        if (current is Screen.Home || current is Screen.SignIn || current is Screen.Pin || current is Screen.ChoosePin) return false
        val prev = back.removeLastOrNull() ?: Screen.Home(HomeTab.PIPELINE)
        _screen.value = prev
        when (prev) {
            is Screen.Home -> refresh()
            is Screen.Order -> reloadOrder()
            is Screen.Deliver -> reloadOrder()
            is Screen.ReceiptView -> openReceipt(prev.id)
            else -> Unit
        }
        return true
    }

    private fun home() {
        back.clear()
        session.ensureWarehouse()
        _screen.value = Screen.Home(HomeTab.PIPELINE)
        refresh()
        SyncWorker.kick(app)
        app.sendPushToken()
        checkUpdate(false)
    }

    fun say(text: String?) { message.value = text }

    /** Runs a server call; a PIN or sign-in it needs moves to that screen. */
    private fun call(block: suspend () -> Unit) {
        viewModelScope.launch {
            busy.value = true
            try {
                block()
            } catch (e: AuthNeeded) {
                if (e.signedOut) signedOut() else lock()
            } catch (e: IOException) {
                say("No connection - try again when the signal is back.")
            } catch (e: Exception) {
                say(e.message ?: "Something went wrong.")
            } finally {
                busy.value = false
            }
        }
    }

    private suspend fun io(block: () -> Reply): Reply = withContext(Dispatchers.IO) { block() }

    // --- signing in, PIN, idle lock -----------------------------------------------------------------

    fun signIn(code: String, loginId: String, password: String) = call {
        val reply = io {
            api.post("/auth/login", JSONObject().put("business_code", code.trim()).put("login_id", loginId.trim())
                .put("password", password).put("device_name", session.deviceName)
                .put("app_version", com.arfa.warehouse.BuildConfig.VERSION_NAME), auth = false)
        }
        if (!reply.ok) return@call say(reply.error)
        session.businessCode = code.trim()
        session.loginId = loginId.trim()
        session.refreshToken = reply.json.getString("refresh_token")
        session.accessToken = reply.json.getString("access_token")
        session.me = reply.json
        session.fcmTokenSent = null
        session.lastActive = System.currentTimeMillis()
        if (!session.pinSet) _screen.value = Screen.ChoosePin else home()
    }

    fun choosePin(pin: String, again: String) = call {
        if (pin != again) return@call say("The two PINs aren't the same.")
        val reply = io { api.put("/auth/pin", JSONObject().put("pin", pin)) }
        if (!reply.ok) return@call say(reply.error)
        session.me = session.me?.put("pin_set", true)
        home()
    }

    fun unlock(pin: String) = call {
        val reply = io {
            api.post("/auth/pin", JSONObject().put("refresh_token", session.refreshToken ?: "").put("pin", pin), auth = false)
        }
        if (reply.signedOut) return@call signedOut(reply.error)
        if (!reply.ok) return@call say(reply.error)
        session.accessToken = reply.json.getString("access_token")
        session.me = reply.json
        session.lastActive = System.currentTimeMillis()
        home()
    }

    fun lock() {
        session.accessToken = null
        back.clear()
        _screen.value = if (session.signedIn) Screen.Pin else Screen.SignIn
    }

    private fun signedOut(why: String? = null) {
        session.signOut()
        back.clear()
        _screen.value = Screen.SignIn
        say(why ?: "Signed out - sign in again.")
    }

    fun signOut() = call {
        try { io { api.post("/auth/logout") } } catch (e: Exception) { }
        signedOut("Signed out.")
    }

    /** Called when the app comes back to the front. */
    fun resumed() {
        val idle = System.currentTimeMillis() - session.lastActive
        val locked = _screen.value is Screen.Pin || _screen.value is Screen.SignIn || _screen.value is Screen.ChoosePin
        if (!locked && session.signedIn && idle > session.idleLockMinutes * 60_000L) lock()
        else if (!locked) refreshCurrent()
    }

    fun touched() { session.lastActive = System.currentTimeMillis() }

    fun chooseWarehouse(id: Long) {
        session.warehouseId = id
        refresh()
    }

    // --- the pipeline -----------------------------------------------------------------------------------

    fun refreshCurrent() {
        when (val s = _screen.value) {
            is Screen.Home -> refresh()
            is Screen.Order -> reloadOrder()
            is Screen.Deliver -> reloadOrder()
            is Screen.ReceiptView -> openReceipt(s.id)
            else -> Unit
        }
    }

    fun refresh() {
        val tab = (_screen.value as? Screen.Home)?.tab ?: return
        viewModelScope.launch {
            try {
                if (tab == HomeTab.RECEIVE) {
                    val r = io { api.get("/receipts?location_id=${session.warehouseId}") }
                    if (r.ok) receipts.value = r.json.optJSONArray("receipts")?.let { a ->
                        (0 until a.length()).map { Receipt.from(a.getJSONObject(it)) } } ?: emptyList()
                    val c = io { api.get("/counts?location_id=${session.warehouseId}") }
                    if (c.ok) counts.value = c.json.optJSONArray("counts")?.let { a ->
                        (0 until a.length()).map { CountInfo.from(a.getJSONObject(it)) } } ?: emptyList()
                    return@launch
                }
                val name = when (tab) {
                    HomeTab.MINE -> "mine"; HomeTab.DONE -> "done"; HomeTab.DELIVER -> "deliver"; else -> "pipeline"
                }
                val r = io { api.get("/pipeline?location_id=${session.warehouseId}&tab=$name") }
                if (r.ok) {
                    val list = r.json.optJSONArray("orders")
                    orders.value = if (list == null) emptyList() else (0 until list.length()).map { OrderCard.from(list.getJSONObject(it)) }
                } else say(r.error)
            } catch (e: AuthNeeded) {
                if (e.signedOut) signedOut() else lock()
            } catch (e: IOException) { /* keep showing what we have */ }
        }
    }

    fun tab(tab: HomeTab) {
        orders.value = emptyList()
        _screen.value = Screen.Home(tab)
        refresh()
    }

    fun claim(id: Long) = call {
        val reply = io { api.post("/orders/$id/claim") }
        if (!reply.ok) {
            say(reply.error)
            refresh()
            return@call
        }
        go(Screen.Order(id))
    }

    // --- one order -----------------------------------------------------------------------------------------

    private fun openOrder(id: Long) {
        order.value = null
        reloadOrder(id)
    }

    fun reloadOrder(id: Long? = null) {
        val orderId = id ?: (order.value?.card?.id ?: (_screen.value as? Screen.Order)?.id
            ?: (_screen.value as? Screen.Deliver)?.id) ?: return
        viewModelScope.launch {
            try {
                val reply = io { api.get("/orders/$orderId") }
                if (reply.ok) {
                    withContext(Dispatchers.IO) {
                        db.orders().save(CachedOrder(orderId, reply.json.toString(), System.currentTimeMillis()))
                    }
                    order.value = OrderDetail.from(reply.json)
                    orderOffline.value = false
                } else say(reply.error)
            } catch (e: AuthNeeded) {
                if (e.signedOut) signedOut() else lock()
            } catch (e: IOException) {
                val cached = withContext(Dispatchers.IO) { db.orders().get(orderId) }
                if (cached != null) {
                    order.value = OrderDetail.from(JSONObject(cached.json))
                    orderOffline.value = true
                } else say("No connection - this order hasn't been opened on this phone yet.")
            }
        }
    }

    fun queueFor(orderId: Long): Flow<List<Pending>> = db.outbox().watch("pick", orderId)
    fun unitsQueueFor(receiptId: Long): Flow<List<Pending>> = db.outbox().watch("unit", receiptId)
    fun countQueueFor(countId: Long): Flow<List<Pending>> = db.outbox().watch("count", countId)
    val waiting: Flow<Int> = db.outbox().waitingCount()

    fun release(id: Long) = call {
        val reply = io { api.post("/orders/$id/release") }
        say(if (reply.ok) "Back in the pipeline - your scans are undone." else reply.error)
        if (reply.ok) { back.clear(); _screen.value = Screen.Home(HomeTab.PIPELINE); refresh() }
    }

    fun confirm(id: Long) = call {
        val reply = io { api.post("/orders/$id/confirm") }
        if (!reply.ok) return@call say(reply.error)
        say(reply.json.optString("message", "Sent for invoicing."))
        back.clear()
        _screen.value = Screen.Home(HomeTab.DONE)
        refresh()
    }

    fun cantFind(id: Long, lineId: Long, reason: String) = call {
        val reply = io { api.post("/orders/$id/short", JSONObject().put("line_id", lineId).put("reason", reason)) }
        say(if (reply.ok) "The showroom is told." else reply.error)
        reloadOrder()
    }

    fun undo(pickId: Long) = call {
        val reply = io { api.delete("/picks/$pickId") }
        if (!reply.ok) say(reply.error)
        reloadOrder()
    }

    fun undoQueued(clientId: String) {
        viewModelScope.launch(Dispatchers.IO) { db.outbox().remove(clientId) }
    }

    /** A read from the scanner against an order line: checked on the phone
     * against this warehouse's serials, then queued for the server. */
    fun pickScan(target: ScanTarget.Pick, serial: String, method: String, photo: File?, allowNew: Boolean,
                 queue: List<Pending>): Feedback {
        touched()
        val detail = order.value ?: return Feedback(false, "Open the order again.")
        val line = detail.lines.firstOrNull { it.id == target.lineId } ?: return Feedback(false, "Line not found.")
        val serverSerials = line.picks.filter { it.ok }.map { it.serial }
        val view = QueueRules.line(line.qty, line.picked, queue.map { QueueRules.Pick(it.clientId, it.lineId, it.serial, it.state) }, line.id)
        if (!view.needsScan) return Feedback(false, "Every unit of ${line.description} is scanned.")
        val clean = Matcher.normalise(serial)
        if (line.product?.trackSerial == false) {
            // No serials on this item: each scan of its code (or a tap) is one unit.
            SyncWorker.queue(app, Pending(clientId = session.nextClientId("p"), kind = "pick", parentId = target.orderId,
                lineId = line.id, serial = clean, method = method, photoPath = photo?.path))
            return Feedback(true, "One ${line.description} picked.")
        }
        if (clean.isEmpty()) return Feedback(false, "Nothing was read - scan the label again.")
        if (QueueRules.alreadyScanned(queue.filter { it.lineId == line.id || it.serial == clean }
                .map { QueueRules.Pick(it.clientId, it.lineId, it.serial, it.state) }, serverSerials, clean)) {
            return Feedback(false, "$clean is already scanned on this order.")
        }
        val known = line.allowed.contains(clean) || line.allowed.any { Matcher.normalise(it) == clean }
        if (!known && !allowNew && !line.truncated && line.product?.trackSerial != false) {
            return Feedback(false, "$clean isn't in stock here for ${line.description}.", offerAdd = clean)
        }
        val item = Pending(clientId = session.nextClientId("p"), kind = "pick", parentId = target.orderId,
            lineId = line.id, serial = clean, method = method, allowNew = allowNew && !known, photoPath = photo?.path)
        SyncWorker.queue(app, item)
        return Feedback(true, if (allowNew && !known) "$clean added and picked." else "$clean picked.")
    }

    fun readLabel(photo: File, orderId: Long?, done: (String?, String) -> Unit) = call {
        val fields = if (orderId != null) mapOf("order_id" to orderId.toString()) else emptyMap()
        val reply = io { api.upload("/label/extract", photo, fields) }
        if (!reply.ok) return@call done(null, reply.error)
        val serial = reply.json.optString("serial_no")
        done(serial.ifEmpty { null }, if (serial.isEmpty()) "ARFA couldn't read a serial on that label." else "ARFA read $serial")
    }

    // --- receiving and counting --------------------------------------------------------------------------

    fun openReceipt(id: Long) = call {
        val reply = io { api.get("/receipts/$id") }
        if (reply.ok) receipt.value = Receipt.from(reply.json) else say(reply.error)
    }

    /** The rack being filled (putaway while receiving, or Move). */
    val currentRack = MutableStateFlow<String?>(null)

    fun setRack(code: String?) { currentRack.value = code?.removePrefix(RACK_PREFIX)?.trim()?.uppercase()?.ifEmpty { null } }

    fun unitScan(target: ScanTarget.Unit, serial: String, method: String, photo: File?, queue: List<Pending>): Feedback {
        touched()
        if (serial.trim().uppercase().startsWith(RACK_PREFIX)) {
            setRack(serial)
            return Feedback(true, "Putting units on rack ${currentRack.value} - scan them.")
        }
        val r = receipt.value ?: return Feedback(false, "Open the receipt again.")
        val line = r.lines.firstOrNull { it.id == target.lineId } ?: return Feedback(false, "Line not found.")
        val clean = Matcher.normalise(serial)
        val codes = Matcher.productCodes(line.product?.modelNo, line.product?.barcode)
        if (Matcher.compact(clean) in codes) return Feedback(false, "That's the model number - scan the serial.")
        val waiting = queue.count { it.lineId == line.id && it.state != QueueRules.REJECTED }
        if (line.units.any { it.serial == clean } || queue.any { it.serial == clean && it.state != QueueRules.REJECTED }) {
            return Feedback(false, "$clean is already scanned.")
        }
        if (line.received + waiting >= line.qty) return Feedback(false, "All ${line.qty} are scanned.")
        val rack = currentRack.value
        SyncWorker.queue(app, Pending(clientId = session.nextClientId("u"), kind = "unit", parentId = r.id,
            lineId = line.id, serial = clean, method = method, photoPath = photo?.path, rack = rack))
        return Feedback(true, "$clean received" + (rack?.let { " - rack $it" } ?: "") + ".")
    }

    fun startCount() = call {
        val reply = io { api.post("/counts", JSONObject().put("location_id", session.warehouseId)) }
        if (!reply.ok) return@call say(reply.error)
        count.value = CountInfo.from(reply.json)
        go(Screen.CountView(count.value!!.id))
    }

    fun openCount(c: CountInfo) {
        count.value = c
        go(Screen.CountView(c.id))
    }

    fun countScan(countId: Long, serial: String, queue: List<Pending>): Feedback {
        touched()
        val clean = Matcher.normalise(serial)
        if (queue.any { it.serial == clean }) return Feedback(false, "$clean is already counted.")
        SyncWorker.queue(app, Pending(clientId = session.nextClientId("c"), kind = "count", parentId = countId,
            lineId = 0, serial = clean, method = "barcode"))
        return Feedback(true, "$clean counted.")
    }

    fun finishCount(countId: Long) = call {
        val reply = io { api.post("/counts/$countId/finish") }
        if (!reply.ok) return@call say(reply.error)
        val j = reply.json
        say("Count finished: ${j.optInt("matched")} matched, ${j.optInt("missing")} not found, " +
            "${j.optInt("extra")} not in ARFA here. The full list is on the website.")
        back.clear()
        _screen.value = Screen.Home(HomeTab.RECEIVE)
        refresh()
    }

    // --- updates -------------------------------------------------------------------------------------------

    fun checkUpdate(manual: Boolean) {
        val now = System.currentTimeMillis()
        if (!manual && now - session.lastUpdateCheck < 12 * 3600_000L) return
        viewModelScope.launch {
            try {
                val reply = io { api.get("/app/version") }
                session.lastUpdateCheck = now
                val code = reply.json.optInt("versionCode", 0)
                if (reply.ok && code > com.arfa.warehouse.BuildConfig.VERSION_CODE) {
                    update.value = reply.json.optString("versionName", code.toString()) to reply.json.optString("apk")
                } else if (manual) {
                    say("You have the newest version (${com.arfa.warehouse.BuildConfig.VERSION_NAME}).")
                }
            } catch (e: Exception) {
                if (manual) say("Couldn't check for an update just now.")
            }
        }
    }

    fun emptyQueue(): Flow<List<Pending>> = flowOf(emptyList())

    // --- the delivery check, swaps, Move (EMI stage) -------------------------------------------------------

    private fun later(block: suspend () -> Unit) {
        viewModelScope.launch {
            try { block() } catch (e: AuthNeeded) {
                if (e.signedOut) signedOut() else lock()
            } catch (e: IOException) {
                say("No connection - the delivery check needs signal. Try again.")
            }
        }
    }

    /** A unit scanned at dispatch - checked by the server at once. */
    fun deliverScan(orderId: Long, serial: String, done: (Feedback) -> Unit) = later {
        touched()
        val reply = io { api.post("/orders/$orderId/deliver-scans", JSONObject().put("serial_no", serial)
            .put("client_scan_id", session.nextClientId("d"))) }
        if (!reply.ok) return@later done(Feedback(false, reply.error))
        done(Feedback(reply.json.optBoolean("ok"), reply.json.optString("message")))
        reloadOrder(orderId)
    }

    fun deliver(orderId: Long) = call {
        val reply = io { api.post("/orders/$orderId/deliver") }
        if (!reply.ok) return@call say(reply.error)
        say(reply.json.optString("message", "Delivered."))
        back.clear()
        _screen.value = Screen.Home(HomeTab.DELIVER)
        refresh()
    }

    fun requestSwap(orderId: Long, old: String, new: String, reason: String) = call {
        val reply = io { api.post("/orders/$orderId/swaps", JSONObject().put("old_serial", old).put("new_serial", new)
            .put("reason", reason)) }
        say(if (reply.ok) reply.json.optString("message") else reply.error)
        reloadOrder(orderId)
    }

    /** Move: a unit onto the rack being filled. A rack label changes the rack. */
    fun moveScan(serial: String, done: (Feedback) -> Unit) = later {
        touched()
        if (serial.trim().uppercase().startsWith(RACK_PREFIX)) {
            setRack(serial)
            return@later done(Feedback(true, "Rack ${currentRack.value} - now scan the units you put there."))
        }
        val rack = currentRack.value ?: return@later done(Feedback(false, "Scan the rack label first."))
        val reply = io { api.post("/bins/move", JSONObject().put("serial_no", serial).put("rack", rack)) }
        done(Feedback(reply.ok, if (reply.ok) reply.json.optString("message") else reply.error))
    }
}
