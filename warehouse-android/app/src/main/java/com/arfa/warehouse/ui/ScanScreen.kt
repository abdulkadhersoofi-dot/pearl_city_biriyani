package com.arfa.warehouse.ui

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Bitmap
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import com.arfa.warehouse.WarehouseApp
import com.arfa.warehouse.data.Pending
import com.arfa.warehouse.scan.CameraView
import com.arfa.warehouse.scan.Frame
import com.arfa.warehouse.scan.Matcher
import com.arfa.warehouse.scan.Photos
import com.arfa.warehouse.scan.Signals
import kotlinx.coroutines.delay
import java.io.File

private const val TEXT_AFTER_MS = 2000L      // barcodes first; text reading after this long without a match
private const val TEXT_EVERY_MS = 700L
private const val HOLD_MS = 1400L            // after a result, before the next read counts

/** A full-screen scanner for one order line, one receipt line or a count.
 * It scans continuously, locks on a match, flashes green (beep, buzz) or red
 * (double buzz, and why), and keeps the label frame as the proof photo. */
@Composable
fun ScanScreen(vm: AppViewModel, target: ScanTarget) {
    val context = LocalContext.current
    val app = context.applicationContext as WarehouseApp
    val session = app.session
    val signals = remember { Signals(context) }
    DisposableEffect(Unit) { onDispose { signals.release() } }

    var permitted by remember {
        mutableStateOf(ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED)
    }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { permitted = it }
    LaunchedEffect(Unit) { if (!permitted) ask.launch(Manifest.permission.CAMERA) }

    val detail by vm.order.collectAsState()
    val receipt by vm.receipt.collectAsState()
    val queue by remember(target) {
        when (target) {
            is ScanTarget.Pick -> vm.queueFor(target.orderId)
            is ScanTarget.Unit -> vm.unitsQueueFor(target.receiptId)
            is ScanTarget.Count -> vm.countQueueFor(target.countId)
            else -> vm.emptyQueue()
        }
    }.collectAsState(initial = emptyList<Pending>())
    val rack by vm.currentRack.collectAsState()
    // Delivery check / Locate: the invoiced serials still to go.
    val targets = remember(detail, target) {
        if (target is ScanTarget.Deliver || target is ScanTarget.Locate) detail?.deliver?.filter { !it.checked } ?: emptyList()
        else emptyList()
    }
    val targetIndex = remember(targets) { Matcher.Index(targets.map { it.serial }) }

    // What this scan is for.
    val line = (target as? ScanTarget.Pick)?.let { t -> detail?.lines?.firstOrNull { it.id == t.lineId } }
    val rLine = (target as? ScanTarget.Unit)?.let { t -> receipt?.lines?.firstOrNull { it.id == t.lineId } }
    val serialLine = line?.product?.trackSerial != false
    val index = remember(line?.allowed) { Matcher.Index(line?.allowed ?: emptyList()) }
    val codes = remember(line, rLine) {
        val p = line?.product
        if (p != null) Matcher.productCodes(p.modelNo, p.barcode, p.sku)
        else Matcher.productCodes(rLine?.product?.modelNo, rLine?.product?.barcode)
    }
    val title = when (target) {
        is ScanTarget.Pick -> line?.description ?: "Scan"
        is ScanTarget.Unit -> rLine?.description ?: "Receive"
        is ScanTarget.Count -> "Count"
        is ScanTarget.Deliver -> "Delivery check"
        is ScanTarget.Locate -> "Locate"
        ScanTarget.Move -> "Move to a rack"
    }
    val modelNo = line?.product?.modelNo ?: rLine?.product?.modelNo
    val subtitle = when (target) {
        is ScanTarget.Unit, ScanTarget.Move -> rack?.let { "Rack $it" } ?: "Scan a rack label to put units on it"
        is ScanTarget.Locate -> "Looking for ${targets.joinToString { it.serial }}"
        else -> modelNo?.let { "Model $it" }
    }

    var feedback by remember { mutableStateOf<Feedback?>(null) }
    var torch by remember { mutableStateOf(false) }
    var heldUntil by remember { mutableStateOf(0L) }
    var started by remember { mutableStateOf(System.currentTimeMillis()) }
    var lastText by remember { mutableStateOf(0L) }
    var offer by remember { mutableStateOf<Pair<String, File?>?>(null) }      // add a serial ARFA doesn't have
    var choices by remember { mutableStateOf<List<String>>(emptyList()) }      // several codes on one label
    var manual by remember { mutableStateOf(false) }
    var reading by remember { mutableStateOf(false) }
    var lastFrame by remember { mutableStateOf<Bitmap?>(null) }
    val queueNow by rememberUpdatedState(queue)

    fun show(f: Feedback) {
        feedback = f
        if (f.ok) signals.good(session.sound) else signals.bad(session.sound)
        heldUntil = System.currentTimeMillis() + HOLD_MS
        started = heldUntil
    }

    /** A serial (or code) read by any means, with the frame it came from. */
    fun take(value: String, method: String, frame: Bitmap?, allowNew: Boolean = false) {
        when (target) {
            is ScanTarget.Deliver -> {
                heldUntil = System.currentTimeMillis() + HOLD_MS
                return vm.deliverScan(target.orderId, value) { show(it) }
            }
            ScanTarget.Move -> {
                heldUntil = System.currentTimeMillis() + HOLD_MS
                return vm.moveScan(value) { show(it) }
            }
            is ScanTarget.Locate -> {
                val found = targets.firstOrNull { Matcher.compact(it.serial) == Matcher.compact(value) } ?: return
                return show(Feedback(true, "Found ${found.serial}" + (if (found.rack.isNotEmpty()) " (${found.rack})" else "")))
            }
            else -> Unit
        }
        val photo = if (target is ScanTarget.Count) null else Photos.save(context, frame)
        val result = when (target) {
            is ScanTarget.Pick -> vm.pickScan(target, value, method, photo, allowNew, queueNow)
            is ScanTarget.Unit -> vm.unitScan(target, value, method, photo, queueNow)
            is ScanTarget.Count -> vm.countScan(target.countId, value, queueNow)
            else -> return
        }
        if (!result.ok) photo?.delete()
        if (!result.ok && result.offerAdd != null) {
            offer = result.offerAdd to Photos.save(context, frame)
        }
        show(result)
    }

    fun onFrame(frame: Frame) {
        val now = System.currentTimeMillis()
        if (now < heldUntil || offer != null || choices.isNotEmpty() || manual || reading) return
        if (frame.text != null) lastText = now
        when (target) {
            is ScanTarget.Pick -> {
                if (line == null) return
                if (!serialLine) {
                    // No serials on this item: its own barcode / model no. counts one unit.
                    frame.barcodes.firstOrNull { Matcher.compact(it) in codes }?.let { take(it, "barcode", frame.snapshot()) }
                    return
                }
                when (val o = Matcher.fromBarcodes(frame.barcodes, index, codes)) {
                    is Matcher.Outcome.Match -> return take(o.serial, "barcode", frame.snapshot())
                    is Matcher.Outcome.RightModel -> {
                        val snap = frame.snapshot()
                        lastFrame = snap
                        if (o.candidate != null) {
                            offer = o.candidate to Photos.save(context, snap)
                            show(Feedback(false, "Right model - serial ${o.candidate} isn't in stock here."))
                        } else {
                            show(Feedback(false, "Right model - now aim at the serial number."))
                        }
                        return
                    }
                    Matcher.Outcome.None -> Unit
                }
                val text = frame.text ?: return
                when (val o = Matcher.fromText(text, index, codes)) {
                    is Matcher.Outcome.Match -> take(o.serial, "photo", frame.snapshot())
                    else -> lastFrame = frame.snapshot()
                }
            }
            is ScanTarget.Deliver, is ScanTarget.Locate -> {
                // Known serials: an exact match wins; for the delivery check a single other code is
                // sent too (the server blocks it and says why); Locate stays quiet on other boxes.
                frame.barcodes.firstNotNullOfOrNull { targetIndex.find(it) }?.let { return take(it, "barcode", null) }
                frame.text?.let { t ->
                    (Matcher.fromText(t, targetIndex, emptySet()) as? Matcher.Outcome.Match)?.let { return take(it.serial, "photo", null) }
                }
                if (target is ScanTarget.Deliver) {
                    val others = frame.barcodes.map { Matcher.normalise(it) }.distinct().filter { v ->
                        val c = Matcher.compact(v)
                        c.length >= 4 && !(c.length == 13 && c.all { it.isDigit() })
                    }
                    if (others.size == 1) take(others[0], "barcode", null)
                }
            }
            else -> {
                // A rack label first: units after it go on that rack (putaway / Move).
                frame.barcodes.firstOrNull { it.trim().uppercase().startsWith(com.arfa.warehouse.ui.RACK_PREFIX) }?.let {
                    if (target is ScanTarget.Unit || target == ScanTarget.Move) {
                        if (target == ScanTarget.Move) take(it, "barcode", null) else {
                            vm.setRack(it)
                            show(Feedback(true, "Putting units on rack ${vm.currentRack.value} - scan them."))
                        }
                        return
                    }
                }
                // Receiving and counting: no list to match - the serial is the code that isn't the
                // model no. / barcode / EAN. More than one: the picker taps the right one.
                val candidates = frame.barcodes.map { Matcher.normalise(it) }.distinct().filter { v ->
                    val c = Matcher.compact(v)
                    c !in codes && c.length >= 4 && !(c.length == 13 && c.all { it.isDigit() })
                }
                when {
                    candidates.size == 1 -> take(candidates[0], "barcode", frame.snapshot())
                    candidates.size > 1 -> { lastFrame = frame.snapshot(); choices = candidates }
                }
            }
        }
    }

    fun wantText(): Boolean {
        val now = System.currentTimeMillis()
        val reads = (target is ScanTarget.Pick && serialLine) || target is ScanTarget.Deliver || target is ScanTarget.Locate
        return reads && now - started > TEXT_AFTER_MS && now - lastText > TEXT_EVERY_MS
    }

    // A rugged scanner phone's own scan button (Zebra DataWedge / Honeywell, set to send
    // the action com.arfa.warehouse.SCAN), checked exactly like a camera barcode.
    if (session.hardwareScanner) {
        DisposableEffect(target) {
            val receiver = object : BroadcastReceiver() {
                override fun onReceive(c: Context, intent: Intent) {
                    val value = listOf("com.symbol.datawedge.data_string", "data", "barcode_string", "barcodeData")
                        .firstNotNullOfOrNull { intent.getStringExtra(it) }?.trim()
                    if (!value.isNullOrEmpty()) take(value, "scanner", lastFrame)
                }
            }
            ContextCompat.registerReceiver(context, receiver, IntentFilter("com.arfa.warehouse.SCAN"),
                ContextCompat.RECEIVER_EXPORTED)
            onDispose { context.unregisterReceiver(receiver) }
        }
    }

    Column(Modifier.fillMaxSize().background(Color.Black)) {
        WarehouseBar(title, subtitle, onBack = { vm.back() }) {
            TextButton({ torch = !torch }) { Text(if (torch) "Torch off" else "Torch", color = Color.White) }
        }
        Box(Modifier.weight(1f).fillMaxWidth()) {
            if (permitted) {
                CameraView(Modifier.fillMaxSize(), torch, paused = offer != null || manual, wantText = ::wantText,
                    onFrame = ::onFrame)
            } else {
                Text("ARFA Warehouse needs the camera to scan labels.", color = Color.White,
                    modifier = Modifier.align(Alignment.Center).padding(24.dp))
            }
            feedback?.let { f ->
                Box(Modifier.align(Alignment.BottomCenter).fillMaxWidth()
                    .background(if (f.ok) Good.copy(alpha = 0.92f) else Bad.copy(alpha = 0.92f)).padding(18.dp)) {
                    Text((if (f.ok) "✓ " else "✗ ") + f.message, color = Color.White, fontSize = 20.sp, fontWeight = FontWeight.Bold)
                }
            }
            if (reading) Text("ARFA is reading the label...", color = Color.White,
                modifier = Modifier.align(Alignment.TopCenter).background(Color.Black.copy(alpha = 0.6f)).padding(12.dp))
        }
        // Progress and the other ways to read.
        Column(Modifier.background(MaterialTheme.colorScheme.surface).padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            val progress = when (target) {
                is ScanTarget.Pick -> line?.let { l ->
                    val waiting = queue.count { it.lineId == l.id && it.state == "queued" }
                    "${minOf(l.qty, l.picked + waiting)} of ${l.qty} scanned"
                }
                is ScanTarget.Unit -> rLine?.let { l ->
                    val waiting = queue.count { it.lineId == l.id && it.state != "rejected" }
                    "${minOf(l.qty, l.received + waiting)} of ${l.qty} received"
                }
                is ScanTarget.Count -> "${queue.size} counted on this phone"
                is ScanTarget.Deliver -> "${detail?.deliver?.count { it.checked } ?: 0} of ${detail?.deliver?.size ?: 0} checked"
                is ScanTarget.Locate -> if (targets.isEmpty()) "Nothing left to find" else "Scan box after box - it beeps on the one"
                ScanTarget.Move -> rack?.let { "Scanning units onto $it" } ?: "Scan the rack label first"
            }
            if (progress != null) Text(progress, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
            if (session.hardwareScanner) WedgeInput { take(it, "scanner", lastFrame) }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.horizontalScroll(rememberScrollState())) {
                if (target is ScanTarget.Pick && serialLine) {
                    OutlinedButton({
                        val frame = lastFrame
                        val file = Photos.save(context, frame)
                        if (file == null) {
                            show(Feedback(false, "Hold the label in view a moment, then try again."))
                        } else {
                            reading = true
                            vm.readLabel(file, target.orderId) { serial, msg ->
                                reading = false
                                if (serial == null) show(Feedback(false, msg))
                                else take(serial, "photo", frame)
                                file.delete()
                            }
                        }
                    }) { Text("Read label with ARFA") }
                }
                if (target is ScanTarget.Pick && !serialLine) {
                    Button({ take("", "manual", lastFrame) }) { Text("Picked one") }
                }
                if (session.manualSerial && !(target is ScanTarget.Pick && !serialLine)) {
                    OutlinedButton({ manual = true }) { Text("Type it") }
                }
                TextButton({ vm.back() }) { Text("Done") }
            }
        }
    }

    offer?.let { (serial, photo) ->
        AlertDialog(onDismissRequest = { photo?.delete(); offer = null; heldUntil = System.currentTimeMillis() + HOLD_MS },
            title = { Text("$serial isn't in ARFA") },
            text = { Text("If this box is ${line?.description ?: "the item on the order"}, add the serial to stock here and pick it. " +
                "The owner sees it in the list to check.") },
            confirmButton = {
                Button({
                    offer = null
                    val result = vm.pickScan(target as ScanTarget.Pick, serial, "barcode", photo, true, queueNow)
                    show(result)
                }) { Text("Add it and pick") }
            },
            dismissButton = { TextButton({ photo?.delete(); offer = null }) { Text("Keep scanning") } })
    }

    if (choices.isNotEmpty()) {
        AlertDialog(onDismissRequest = { choices = emptyList() },
            title = { Text("Which is the serial number?") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    choices.forEach { c ->
                        AssistChip(onClick = { choices = emptyList(); take(c, "barcode", lastFrame) }, label = { Text(c, fontSize = 17.sp) })
                    }
                }
            },
            confirmButton = { TextButton({ choices = emptyList() }) { Text("None of these") } })
    }

    if (manual) {
        var typed by remember { mutableStateOf("") }
        AlertDialog(onDismissRequest = { manual = false },
            title = { Text("Type the serial") },
            text = {
                Column {
                    Text("Kept with a photo of the label - hold it in view.")
                    OutlinedTextField(typed, { typed = it.uppercase() }, singleLine = true,
                        keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Characters),
                        modifier = Modifier.padding(top = 8.dp))
                }
            },
            confirmButton = {
                Button({ manual = false; take(typed, "manual", lastFrame) }, enabled = typed.isNotBlank(),
                    colors = ButtonDefaults.buttonColors()) { Text("Use it") }
            },
            dismissButton = { TextButton({ manual = false }) { Text("Cancel") } })
    }
}

/** A keyboard-wedge scanner types the code and presses Enter. */
@Composable
private fun WedgeInput(onScan: (String) -> Unit) {
    var text by remember { mutableStateOf("") }
    val focus = remember { FocusRequester() }
    LaunchedEffect(Unit) { delay(300); runCatching { focus.requestFocus() } }
    OutlinedTextField(text, { value ->
        if (value.endsWith("\n")) {
            value.trim().takeIf { it.isNotEmpty() }?.let(onScan)
            text = ""
        } else text = value
    }, label = { Text("Scanner input") }, singleLine = true,
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
        keyboardActions = KeyboardActions(onDone = { text.trim().takeIf { it.isNotEmpty() }?.let(onScan); text = "" }),
        modifier = Modifier.fillMaxWidth().height(64.dp).focusRequester(focus))
}
