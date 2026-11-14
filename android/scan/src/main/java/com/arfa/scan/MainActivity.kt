package com.arfa.scan

import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.pdf.PdfRenderer
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.ParcelFileDescriptor
import android.view.View
import android.widget.PopupMenu
import android.widget.Toast
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.widget.doAfterTextChanged
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import androidx.recyclerview.widget.LinearLayoutManager
import com.arfa.scan.databinding.ActivityMainBinding
import com.google.mlkit.vision.documentscanner.GmsDocumentScannerOptions
import com.google.mlkit.vision.documentscanner.GmsDocumentScanning
import com.google.mlkit.vision.documentscanner.GmsDocumentScanningResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.util.UUID

/** The chat: every bill sent is a message, newest at the bottom, with
 * ticks and a tag saying how its checks went. The camera button opens the
 * document scanner; the paperclip picks photos or PDFs. */
class MainActivity : AppCompatActivity() {

    private lateinit var b: ActivityMainBinding
    private lateinit var adapter: ChatAdapter
    private val bills = linkedMapOf<Int, Bill>()
    private var serverTime: String? = null
    private var me: JSONObject? = null
    private var filter = "all"
    private var query = ""
    private var replaces: Int? = null
    private var pile = false

    private val scanner = registerForActivityResult(ActivityResultContracts.StartIntentSenderForResult()) { result ->
        val scanned = if (result.resultCode == Activity.RESULT_OK)
            GmsDocumentScanningResult.fromActivityResultIntent(result.data) else null
        val retake = replaces
        replaces = null
        if (scanned != null) take(scanned, retake)
    }

    private val picker = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) addFiles(uris)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Api.session.signedIn) {
            startActivity(Intent(this, LoginActivity::class.java))
            finish()
            return
        }
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)
        replaces = savedInstanceState?.getInt("replaces")?.takeIf { it > 0 }
        pile = savedInstanceState?.getBoolean("pile") ?: false

        adapter = ChatAdapter(
            onOpen = { startActivity(Intent(this, BillActivity::class.java).putExtra("id", it.id)) },
            onRetake = { scan(it.id) },
            onPending = { pendingTapped(it) },
            owner = { me?.optJSONObject("user")?.optBoolean("owner") ?: false },
        )
        b.list.layoutManager = LinearLayoutManager(this).apply { stackFromEnd = true }
        b.list.adapter = adapter
        b.list.itemAnimator = null

        me = Api.session.me
        header()
        b.refresh.setColorSchemeResources(R.color.arfa_primary)
        b.refresh.setOnRefreshListener { lifecycleScope.launch { load(full = true); b.refresh.isRefreshing = false } }
        b.camera.setOnClickListener { scan(null) }
        b.camera.setOnLongClickListener { pile = true; scan(null); true }
        b.attach.setOnClickListener { picker.launch(arrayOf("image/*", "application/pdf")) }
        b.more.setOnClickListener { menu(it) }
        b.search.doAfterTextChanged { query = it?.toString()?.trim()?.lowercase().orEmpty(); render() }
        b.filters.setOnCheckedStateChangeListener { _, ids ->
            filter = when (ids.firstOrNull()) {
                R.id.f_check -> "to_check"; R.id.f_look -> "look"; R.id.f_retake -> "retake"
                R.id.f_posted -> "posted"; else -> "all"
            }
            render()
        }

        // Live: the bills on the phone, and the server's every few seconds while open.
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch { Outbox.changes.collect { render() } }
                launch {
                    load(full = true)
                    while (true) {
                        val busy = Outbox.all().isNotEmpty() || bills.values.any { it.state == "reading" }
                        delay(if (busy) 3000L else 15000L)
                        load(full = false)
                    }
                }
            }
        }
        if (Outbox.all().isNotEmpty()) SendWorker.start(this)
        handleShare(intent)
        UpdateChecker.check(this, manual = false)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putInt("replaces", replaces ?: 0)
        outState.putBoolean("pile", pile)
    }

    override fun onResume() {
        super.onResume()
        lifecycleScope.launch { load(full = false) }
    }

    // --- the server's bills ----------------------------------------------------------------------

    private suspend fun load(full: Boolean) {
        try {
            val since = serverTime
            val body = withContext(Dispatchers.IO) {
                Api.get(if (full || since == null) "/bills" else "/bills?since=" + Uri.encode(since))
            }
            if (full || since == null) bills.clear()
            val list = body.optJSONArray("bills")
            for (i in 0 until (list?.length() ?: 0)) {
                val bill = Bill.from(list!!.getJSONObject(i))
                if (bill.state == "discarded" || bill.status == "split") bills.remove(bill.id) else bills[bill.id] = bill
            }
            serverTime = body.optString("server_time").ifEmpty { serverTime }
            body.optJSONObject("me")?.let { me = it; Api.session.me = it; header() }
            b.banner.visibility = View.GONE
            render()
        } catch (e: ApiError) {
            if (e.signedOut) signedOut() else banner(e.message ?: "")
        } catch (e: IOException) {
            banner(getString(R.string.offline))
        }
    }

    private fun banner(text: String) {
        b.banner.text = text
        b.banner.visibility = if (text.isEmpty()) View.GONE else View.VISIBLE
    }

    private fun header() {
        val m = me ?: return
        val name = m.optJSONObject("business")?.optString("name").orEmpty()
        val user = m.optJSONObject("user")
        b.title.text = name.ifEmpty { getString(R.string.app_name) }
        b.avatar.text = name.split(" ").filter { it.isNotEmpty() }.take(2).joinToString("") { it.take(1) }.uppercase()
        val c = m.optJSONObject("counts")
        val parts = mutableListOf<String>()
        c?.optInt("to_check")?.takeIf { it > 0 }?.let { parts += "$it to check" }
        c?.optInt("reading")?.takeIf { it > 0 }?.let { parts += "$it reading" }
        c?.optInt("retake")?.takeIf { it > 0 }?.let { parts += "$it to retake" }
        if (user != null && !user.optBoolean("owner")) parts += user.optString("name")
        b.subtitle.text = parts.joinToString(" · ").ifEmpty { "Tap the camera to scan a bill" }
    }

    private fun render() {
        if (!::adapter.isInitialized) return
        val server = bills.values.filter { bill ->
            val shown = when (filter) {
                "to_check" -> bill.status == "to_check"
                "look" -> bill.state == "look" || bill.state == "duplicate"
                "retake" -> bill.state == "retake"
                "posted" -> bill.state == "posted" || bill.state == "filed"
                else -> true
            }
            shown && (query.isEmpty() || query.split(" ").all { word -> bill.searchText.contains(word) ||
                bill.searchText.contains(word.replace(",", "")) })
        }.sortedBy { it.uploadedAt }
        val pending = if (filter == "all" && query.isEmpty()) Outbox.all().sortedBy { it.created } else emptyList()
        val rows = mutableListOf<Row>()
        var lastDay = ""
        val today = getString(R.string.today)
        val yesterday = getString(R.string.yesterday)
        for (bill in server) {
            val day = Format.dayOf(bill.uploadedAt, today, yesterday)
            if (day != lastDay) { rows += Row.Day(day); lastDay = day }
            rows += Row.Server(bill)
        }
        if (pending.isNotEmpty() && lastDay != today) rows += Row.Day(today)
        pending.forEach { rows += Row.Pending(it) }
        val atBottom = !b.list.canScrollVertically(1)
        val grew = rows.size > adapter.itemCount
        adapter.submitList(rows) {
            if (grew && atBottom && rows.isNotEmpty()) b.list.scrollToPosition(rows.size - 1)
        }
        b.empty.visibility = if (rows.isEmpty() && filter == "all" && query.isEmpty()) View.VISIBLE else View.GONE
    }

    // --- taking bills ----------------------------------------------------------------------------------

    /** The document scanner: finds the bill's edges, takes it by itself
     * when the phone is steady, straightens and crops it, more pages. */
    private fun scan(retake: Int?) {
        replaces = retake
        val options = GmsDocumentScannerOptions.Builder()
            .setGalleryImportAllowed(true)
            .setPageLimit(if (retake != null) 10 else 30)
            .setResultFormats(GmsDocumentScannerOptions.RESULT_FORMAT_JPEG, GmsDocumentScannerOptions.RESULT_FORMAT_PDF)
            .setScannerMode(GmsDocumentScannerOptions.SCANNER_MODE_FULL)
            .build()
        GmsDocumentScanning.getClient(options).getStartScanIntent(this)
            .addOnSuccessListener { sender -> scanner.launch(IntentSenderRequest.Builder(sender).build()) }
            .addOnFailureListener { e ->
                pile = false
                Toast.makeText(this, "The scanner couldn't open (${e.message}). Update Google Play services, " +
                    "or pick a photo with the paperclip.", Toast.LENGTH_LONG).show()
            }
    }

    private fun take(result: GmsDocumentScanningResult, retake: Int?) {
        val pages = result.pages?.map { it.imageUri }.orEmpty()
        val pdf = result.pdf?.uri
        val asPile = pile && retake == null
        pile = false
        if (pages.isEmpty()) return
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                if (asPile || pages.size == 1 || pdf == null) {
                    // one page = one bill (a pile: every page its own bill)
                    val groups = if (asPile) pages.map { listOf(it) } else listOf(pages)
                    for (group in groups) {
                        if (group.size == 1) addPhoto(group[0], retake) else addPdfPages(group, retake)
                    }
                } else {
                    addPdf(pdf, pages, retake)
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@MainActivity, e.message ?: "That bill couldn't be saved - try again.", Toast.LENGTH_LONG).show()
                }
            }
            SendWorker.start(applicationContext)
        }
    }

    private fun addPhoto(uri: Uri, retake: Int?) {
        val file = Outbox.newFile("jpg")
        Images.prepareJpeg(this, uri, file)
        val preview = Outbox.newFile("jpg").also { Images.preview(this, Uri.fromFile(file), it) }
        Outbox.add(Outgoing(UUID.randomUUID().toString(), file.path, "image/jpeg", preview.path, retake,
            System.currentTimeMillis(), "waiting"))
    }

    /** Several pages of one bill: the scanner's PDF, and its pages as
     * pictures for reading on the phone. */
    private fun addPdf(pdf: Uri, pages: List<Uri>, retake: Int?) {
        val file = Outbox.newFile("pdf")
        Images.copy(this, pdf, file)
        val preview = Outbox.newFile("jpg").also { Images.preview(this, pages[0], it) }
        val ocrPages = pages.take(4).map { page ->
            Outbox.newFile("jpg").also { Images.prepareJpeg(this, page, it, maxPx = 1800) }.path
        }
        Outbox.add(Outgoing(UUID.randomUUID().toString(), file.path, "application/pdf", preview.path, retake,
            System.currentTimeMillis(), "waiting", ocrPages = ocrPages.joinToString("|")))
    }

    /** Pages without the scanner's PDF: made into one on the phone. */
    private fun addPdfPages(pages: List<Uri>, retake: Int?) {
        val pictures = pages.map { page -> Outbox.newFile("jpg").also { Images.prepareJpeg(this, page, it, maxPx = 2000) } }
        val file = Outbox.newFile("pdf")
        val document = android.graphics.pdf.PdfDocument()
        try {
            pictures.forEachIndexed { i, picture ->
                val bitmap = android.graphics.BitmapFactory.decodeFile(picture.path) ?: return@forEachIndexed
                val page = document.startPage(android.graphics.pdf.PdfDocument.PageInfo.Builder(bitmap.width, bitmap.height, i + 1).create())
                page.canvas.drawBitmap(bitmap, 0f, 0f, null)
                document.finishPage(page)
                bitmap.recycle()
            }
            FileOutputStream(file).use { document.writeTo(it) }
        } finally {
            document.close()
        }
        val preview = Outbox.newFile("jpg").also { Images.preview(this, Uri.fromFile(pictures[0]), it) }
        val keep = pictures.take(4)
        pictures.drop(4).forEach { it.delete() }
        Outbox.add(Outgoing(UUID.randomUUID().toString(), file.path, "application/pdf", preview.path, retake,
            System.currentTimeMillis(), "waiting", ocrPages = keep.joinToString("|") { it.path }))
    }

    private fun addFiles(uris: List<Uri>) {
        lifecycleScope.launch(Dispatchers.IO) {
            for (uri in uris) {
                try {
                    val type = contentResolver.getType(uri) ?: ""
                    if (type == "application/pdf") addPickedPdf(uri) else addPhoto(uri, null)
                } catch (e: Exception) {
                    withContext(Dispatchers.Main) {
                        Toast.makeText(this@MainActivity, e.message ?: "That file couldn't be added.", Toast.LENGTH_LONG).show()
                    }
                }
            }
            SendWorker.start(applicationContext)
        }
    }

    private fun addPickedPdf(uri: Uri) {
        val file = Outbox.newFile("pdf")
        Images.copy(this, uri, file)
        val pictures = mutableListOf<String>()
        var preview: String? = null
        runCatching {
            ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY).use { fd ->
                PdfRenderer(fd).use { renderer ->
                    for (i in 0 until minOf(renderer.pageCount, 4)) {
                        renderer.openPage(i).use { page ->
                            val scale = 1800f / maxOf(page.width, page.height)
                            val bitmap = Bitmap.createBitmap((page.width * scale).toInt(), (page.height * scale).toInt(),
                                Bitmap.Config.ARGB_8888)
                            bitmap.eraseColor(android.graphics.Color.WHITE)
                            page.render(bitmap, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                            val out = Outbox.newFile("jpg")
                            FileOutputStream(out).use { bitmap.compress(Bitmap.CompressFormat.JPEG, 85, it) }
                            bitmap.recycle()
                            pictures += out.path
                        }
                    }
                }
            }
            preview = pictures.firstOrNull()?.let { first ->
                Outbox.newFile("jpg").also { Images.preview(this, Uri.fromFile(File(first)), it) }.path
            }
        }
        Outbox.add(Outgoing(UUID.randomUUID().toString(), file.path, "application/pdf", preview, null,
            System.currentTimeMillis(), "waiting", ocrPages = pictures.joinToString("|")))
    }

    private fun handleShare(intent: Intent?) {
        intent ?: return
        // "Retake" on a bill's page: straight into the scanner, for that bill.
        intent.getIntExtra("retake", 0).takeIf { it > 0 }?.let { bill ->
            intent.removeExtra("retake")
            scan(bill)
            return
        }
        val uris = when (intent.action) {
            Intent.ACTION_SEND -> listOfNotNull(streamOf(intent))
            Intent.ACTION_SEND_MULTIPLE -> streamsOf(intent)
            else -> emptyList()
        }
        if (uris.isNotEmpty()) {
            intent.action = null
            addFiles(uris)
        }
    }

    @Suppress("DEPRECATION")
    private fun streamOf(intent: Intent): Uri? =
        if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else intent.getParcelableExtra(Intent.EXTRA_STREAM)

    @Suppress("DEPRECATION")
    private fun streamsOf(intent: Intent): List<Uri> =
        (if (Build.VERSION.SDK_INT >= 33) intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM)).orEmpty()

    private fun pendingTapped(item: Outgoing) {
        if (item.state != "failed") return
        AlertDialog.Builder(this)
            .setTitle("Not sent")
            .setMessage(item.error ?: "This bill couldn't be sent.")
            .setPositiveButton("Try again") { _, _ ->
                Outbox.update(item.localId) { it.state = "waiting"; it.error = null }
                SendWorker.start(this)
            }
            .setNegativeButton(R.string.discard) { _, _ -> Outbox.remove(item.localId) }
            .show()
    }

    // --- the menu ----------------------------------------------------------------------------------------

    private fun menu(anchor: View) {
        PopupMenu(this, anchor).apply {
            menu.add(0, 1, 0, R.string.scan_pile)
            menu.add(0, 4, 1, R.string.appearance)
            menu.add(0, 2, 2, R.string.check_update)
            menu.add(0, 3, 3, R.string.sign_out)
            setOnMenuItemClickListener {
                when (it.itemId) {
                    1 -> { pile = true; scan(null) }
                    2 -> UpdateChecker.check(this@MainActivity, manual = true)
                    3 -> signOut()
                    4 -> appearance()
                }
                true
            }
        }.show()
    }

    /** Day, night or the phone's own setting - remembered on this phone. */
    private fun appearance() {
        val keys = arrayOf("system", "light", "dark")
        val labels = arrayOf(getString(R.string.theme_system), getString(R.string.theme_light), getString(R.string.theme_dark))
        AlertDialog.Builder(this)
            .setTitle(R.string.appearance)
            .setSingleChoiceItems(labels, keys.indexOf(Api.session.theme).coerceAtLeast(0)) { dialog, which ->
                Api.session.theme = keys[which]
                dialog.dismiss()
                ScanApp.applyTheme(keys[which])
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun signOut() {
        AlertDialog.Builder(this)
            .setTitle(R.string.sign_out)
            .setMessage("Sign out of ARFA Scan on this phone? Bills not sent yet are thrown away.")
            .setPositiveButton(R.string.sign_out) { _, _ ->
                lifecycleScope.launch {
                    runCatching { withContext(Dispatchers.IO) { Api.post("/auth/logout") } }
                    signedOut()
                }
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun signedOut() {
        Api.session.signOut()
        Outbox.clear()
        startActivity(Intent(this, LoginActivity::class.java))
        finish()
    }
}
