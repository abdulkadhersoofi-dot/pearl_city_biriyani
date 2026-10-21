package com.arfa.pos

import android.Manifest
import android.annotation.SuppressLint
import android.app.ActivityManager
import android.app.PendingIntent
import android.bluetooth.BluetoothManager
import android.content.ActivityNotFoundException
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.print.PageRange
import android.print.PrintAttributes
import android.print.PrintDocumentAdapter
import android.print.PrintDocumentInfo
import android.print.PrintManager
import android.provider.Settings
import android.text.InputType
import android.util.Base64
import android.view.View
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.JsPromptResult
import android.webkit.JsResult
import android.webkit.WebChromeClient
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.Toast
import androidx.activity.addCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import com.arfa.pos.databinding.ActivityMainBinding
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * ARFA Counter: a full-screen WebView locked to this app's own ARFA
 * server, opening the app's phone screens (/app/ - pairing with the
 * owner's code, Bill, Tables & KOT, Held, Today, Kitchen). Everything a
 * cashier, waiter or kitchen does is the server-rendered UI; this activity
 * is the native frame around it (splash, offline screen, back button),
 * the "branch logins only" gate, and the things a web page can't do on
 * its own:
 *
 *  - direct ESC/POS printing to a Bluetooth / USB thermal printer, with
 *    the system print dialog (PDF) as the fallback, and the cash drawer
 *  - kiosk mode (Android lock task) so the tablet stays on the POS
 *  - the in-app update check against the "android-latest" release.
 *
 * The app opens /app/?start=1: the web page then signs the phone back in
 * with its device secret if needed and goes to what this phone is for
 * (Counter, Waiter or Kitchen - chosen after pairing). Offline billing and
 * KOTs live in the web page (service worker + queues, pos_offline.js,
 * counter_tables.js); with no connection the page's saved copy opens.
 * The WebView's user agent carries "ARFACounter/<version>" so the server
 * sends a branch login here rather than to the website's counter.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var settings: PrinterSettings
    private lateinit var printer: EscPosPrinter
    private val baseUrl: String = BuildConfig.SERVER_BASE_URL
    private val baseHost: String = Uri.parse(baseUrl).host ?: ""

    // Paths a counter login is allowed to be on. Anything else means the
    // account that just signed in isn't a counter login - see
    // handlePageFinished().
    private val allowedPrefixes = listOf("/app/", "/auth/", "/pos/", "/media/", "/static/", "/healthz", "/restaurant/")
    private val startUrl: String get() = "$baseUrl/app/?start=1"

    private var isHandlingBlockedRole = false
    private var hasLoadedOnce = false
    private var backPressedAt = 0L
    // Keeps the off-screen WebView alive while Android prints from it.
    private var printWebView: WebView? = null

    // Android 12+ asks before an app may talk to Bluetooth devices.
    private val bluetoothPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) chooseBluetoothPrinter() else toast(getString(R.string.bt_permission_needed))
        }

    private val usbPermissionReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action != ACTION_USB_PERMISSION) return
            if (intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)) {
                toast(getString(R.string.usb_ready))
            } else {
                toast(getString(R.string.usb_permission_needed))
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        val splash = installSplashScreen()
        super.onCreate(savedInstanceState)
        splash.setKeepOnScreenCondition { !hasLoadedOnce }

        settings = PrinterSettings(this)
        printer = EscPosPrinter(this, settings)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setupWebView()
        binding.retryButton.setOnClickListener { reload() }
        binding.offlineButton.setOnClickListener { openApp() }
        ContextCompat.registerReceiver(
            this, usbPermissionReceiver, IntentFilter(ACTION_USB_PERMISSION), ContextCompat.RECEIVER_NOT_EXPORTED
        )

        onBackPressedDispatcher.addCallback(this) {
            when {
                binding.webView.canGoBack() -> binding.webView.goBack()
                isAtHome() -> confirmExitOnDoubleBack()
                else -> binding.webView.loadUrl("$baseUrl/app/")
            }
        }

        if (savedInstanceState == null) {
            // Always the app's start: the page pairs a new phone, signs a
            // paired one back in, and opens what this phone is for - its
            // saved copy when there's no connection.
            openApp()
        }
        UpdateChecker.check(this, settings, manual = false)
    }

    override fun onResume() {
        super.onResume()
        if (settings.kioskEnabled) startKiosk()
    }

    override fun onDestroy() {
        try {
            unregisterReceiver(usbPermissionReceiver)
        } catch (_: IllegalArgumentException) {
        }
        super.onDestroy()
    }

    private fun openApp() {
        binding.errorView.visibility = View.GONE
        binding.loadingSpinner.visibility = View.VISIBLE
        binding.webView.loadUrl(startUrl)
    }

    private fun isAtHome(): Boolean {
        val path = Uri.parse(binding.webView.url ?: "").path ?: ""
        return path == "/app/" || path == "/app" || path.startsWith("/auth/login") || path.isEmpty()
    }

    private fun confirmExitOnDoubleBack() {
        if (settings.kioskEnabled) return // the kiosk stays on the POS
        val now = System.currentTimeMillis()
        if (now - backPressedAt < 2000) {
            finish()
        } else {
            backPressedAt = now
            toast(getString(R.string.exit_confirm))
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val webView = binding.webView
        val webSettings = webView.settings
        webSettings.javaScriptEnabled = true
        webSettings.domStorageEnabled = true // localStorage + IndexedDB for the offline bill queue
        webSettings.databaseEnabled = true
        webSettings.useWideViewPort = true
        webSettings.loadWithOverviewMode = true
        webSettings.cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
        // Tells the server this is the ARFA Counter app (app/auth/routes.py
        // APP_AGENT): a branch login lands on the app's screens.
        webSettings.userAgentString = webSettings.userAgentString + " ARFACounter/" + BuildConfig.VERSION_NAME

        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false)

        webView.addJavascriptInterface(PrintBridge(), "AndroidPrint")
        webView.webViewClient = AppWebViewClient()
        webView.webChromeClient = AppChromeClient()
    }

    /**
     * The page's own alert / confirm / prompt boxes. A WebView without this
     * answers every confirm() with "Cancel" and never shows it - which is
     * how Discard on a held bill used to do nothing in the app. The web
     * screens now draw their own boxes (static/js/dialog.js); this is the
     * safety net for any page that still asks the browser.
     */
    private inner class AppChromeClient : WebChromeClient() {
        override fun onJsAlert(view: WebView?, url: String?, message: String?, result: JsResult): Boolean {
            if (isFinishing) { result.cancel(); return true }
            AlertDialog.Builder(this@MainActivity)
                .setMessage(message)
                .setPositiveButton(android.R.string.ok) { _, _ -> result.confirm() }
                .setOnCancelListener { result.confirm() }
                .show()
            return true
        }

        override fun onJsConfirm(view: WebView?, url: String?, message: String?, result: JsResult): Boolean {
            if (isFinishing) { result.cancel(); return true }
            AlertDialog.Builder(this@MainActivity)
                .setMessage(message)
                .setPositiveButton(android.R.string.ok) { _, _ -> result.confirm() }
                .setNegativeButton(android.R.string.cancel) { _, _ -> result.cancel() }
                .setOnCancelListener { result.cancel() }
                .show()
            return true
        }

        override fun onJsPrompt(view: WebView?, url: String?, message: String?, defaultValue: String?,
                                result: JsPromptResult): Boolean {
            if (isFinishing) { result.cancel(); return true }
            val input = EditText(this@MainActivity).apply {
                setText(defaultValue ?: "")
                setSingleLine(false)
            }
            AlertDialog.Builder(this@MainActivity)
                .setMessage(message)
                .setView(input)
                .setPositiveButton(android.R.string.ok) { _, _ -> result.confirm(input.text.toString()) }
                .setNegativeButton(android.R.string.cancel) { _, _ -> result.cancel() }
                .setOnCancelListener { result.cancel() }
                .show()
            return true
        }
    }

    private fun reload() {
        binding.errorView.visibility = View.GONE
        binding.loadingSpinner.visibility = View.VISIBLE
        val current = binding.webView.url
        if (current.isNullOrBlank()) {
            binding.webView.loadUrl(startUrl)
        } else {
            binding.webView.reload()
        }
    }

    private fun showError() {
        binding.webView.visibility = View.INVISIBLE
        binding.loadingSpinner.visibility = View.GONE
        binding.errorView.visibility = View.VISIBLE
        // A paired phone opens its saved screens without internet; one that
        // never paired just sees the same error again.
        binding.offlineButton.visibility = View.VISIBLE
        hasLoadedOnce = true // stop the splash from hanging forever offline
    }

    /**
     * Runs once a page has actually finished loading - the one place
     * this app decides whether the account that's signed in belongs
     * here at all. Deliberately not done in shouldOverrideUrlLoading:
     * a login form's POST isn't reliably reported there on every
     * Android/WebView version, but onPageFinished always fires for
     * whatever page the server actually sent back.
     */
    private fun handlePageFinished(url: String) {
        binding.webView.visibility = View.VISIBLE
        binding.loadingSpinner.visibility = View.GONE
        binding.errorView.visibility = View.GONE
        hasLoadedOnce = true

        val path = Uri.parse(url).path ?: ""
        // The kitchen screen stays lit on its tablet.
        if (path.startsWith("/app/kitchen")) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
        val isAllowed = allowedPrefixes.any { path.startsWith(it) } || path == "/"
        if (isAllowed) {
            isHandlingBlockedRole = false
            return
        }
        if (isHandlingBlockedRole) return // already kicking this session out, don't re-trigger
        isHandlingBlockedRole = true

        AlertDialog.Builder(this)
            .setTitle(R.string.branch_only_title)
            .setMessage(R.string.branch_only_body)
            .setCancelable(false)
            .setPositiveButton(R.string.ok) { dialog, _ ->
                dialog.dismiss()
                binding.webView.loadUrl("$baseUrl/auth/logout")
            }
            .show()
    }

    private inner class AppWebViewClient : WebViewClient() {

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val uri = request.url
            // "Share on WhatsApp" (POS bill sharing) - hand the wa.me
            // click-to-chat link to the phone so WhatsApp opens, rather
            // than loading it inside this WebView.
            if (uri.scheme == "https" && uri.host?.let { it in SHARE_HOSTS } == true) {
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, uri))
                } catch (e: ActivityNotFoundException) {
                    toast(getString(R.string.no_app_to_share))
                }
                return true
            }
            if (uri.scheme != "http" && uri.scheme != "https") return true // tel:/mailto:/etc - not used by this app
            if (uri.host != baseHost) return true // never follow a link off this app's own server
            return false
        }

        override fun onPageStarted(view: WebView, url: String, favicon: android.graphics.Bitmap?) {
            binding.errorView.visibility = View.GONE
            if (!hasLoadedOnce) binding.loadingSpinner.visibility = View.VISIBLE
        }

        override fun onPageFinished(view: WebView, url: String) {
            handlePageFinished(url)
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (request.isForMainFrame) showError()
        }

        override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, errorResponse: WebResourceResponse) {
            // A 4xx on the main document is still a real page Flask
            // rendered (e.g. a 404 page) - only a 5xx means the server
            // itself is in trouble, worth swapping to the native
            // offline screen for.
            if (request.isForMainFrame && errorResponse.statusCode >= 500) showError()
        }

        override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
            handler.cancel() // never silently trust a bad certificate
            showError()
        }
    }

    /**
     * Exposed to the web app's JS as `window.AndroidPrint`. Not
     * `private` on purpose - WebView's addJavascriptInterface() calls
     * this reflectively, and a private declaring class can make that
     * throw IllegalAccessException at runtime even though the method
     * itself is public. Methods run on a WebView thread, never the main
     * one - anything touching the UI goes through runOnUiThread.
     */
    inner class PrintBridge {
        /** The original PDF path (Android's print dialog). */
        @JavascriptInterface
        fun printReceipt(url: String) {
            val absoluteUrl = if (url.startsWith("http")) url else baseUrl + url
            runOnUiThread { downloadAndPrint(absoluteUrl) }
        }

        @JavascriptInterface
        fun hasPrinter(): Boolean = settings.hasPrinter

        @JavascriptInterface
        fun printerColumns(): Int = settings.columns

        /**
         * ESC/POS bytes (base64) straight to the thermal printer. Returns
         * false when no printer is set up, so the page uses its fallback;
         * a printer that then fails calls the page's arfaEscPosFailed().
         */
        @JavascriptInterface
        fun printRaw(base64: String, openDrawer: Boolean): Boolean {
            if (!settings.hasPrinter) return false
            val bytes = try {
                Base64.decode(base64, Base64.DEFAULT)
            } catch (e: IllegalArgumentException) {
                return false
            }
            val payload = if (openDrawer && settings.kickDrawer) bytes + EscPosPrinter.DRAWER_KICK else bytes
            sendToPrinter(payload, onFailJs = "window.arfaEscPosFailed && window.arfaEscPosFailed()")
            return true
        }

        /** A receipt page printed through Android's print dialog (no thermal printer set up). */
        @JavascriptInterface
        fun printHtml(html: String) {
            runOnUiThread { printHtmlPage(html) }
        }

        @JavascriptInterface
        fun openDrawer(): Boolean {
            if (!settings.hasPrinter) return false
            sendToPrinter(EscPosPrinter.DRAWER_KICK, onFailJs = null)
            return true
        }

        @JavascriptInterface
        fun openSettings() {
            runOnUiThread { showAppSettings() }
        }

        @JavascriptInterface
        fun appVersion(): String = BuildConfig.VERSION_NAME
    }

    private fun sendToPrinter(bytes: ByteArray, onFailJs: String?) {
        Thread {
            try {
                printer.send(bytes)
            } catch (e: IOException) {
                runOnUiThread {
                    toast(e.message ?: getString(R.string.printer_failed))
                    if (onFailJs != null) binding.webView.evaluateJavascript(onFailJs, null)
                }
            }
        }.start()
    }

    /**
     * The web app prints a receipt by loading a server-rendered PDF
     * into a hidden iframe and calling window.print() on it - desktop
     * browsers handle that natively, but Android's WebView has no
     * built-in PDF viewer or print dialog to drive that way. Instead,
     * this downloads the same PDF (reusing the WebView's own session
     * cookie) and hands the raw bytes to Android's PrintManager, which
     * shows the real system print dialog.
     */
    private fun downloadAndPrint(url: String) {
        Thread {
            try {
                val connection = URL(url).openConnection() as HttpURLConnection
                val cookie = CookieManager.getInstance().getCookie(url)
                if (!cookie.isNullOrBlank()) connection.setRequestProperty("Cookie", cookie)
                connection.connectTimeout = 15000
                connection.readTimeout = 15000
                connection.connect()
                if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                    throw IOException("HTTP ${connection.responseCode}")
                }
                val bytes = connection.inputStream.use { it.readBytes() }
                runOnUiThread { startPrintJob(bytes) }
            } catch (e: IOException) {
                runOnUiThread { toast(getString(R.string.receipt_load_failed)) }
            }
        }.start()
    }

    private fun startPrintJob(pdfBytes: ByteArray) {
        val printManager = getSystemService(Context.PRINT_SERVICE) as PrintManager
        val adapter = object : PrintDocumentAdapter() {
            override fun onLayout(
                oldAttributes: PrintAttributes?,
                newAttributes: PrintAttributes,
                cancellationSignal: CancellationSignal?,
                callback: PrintDocumentAdapter.LayoutResultCallback,
                extras: Bundle?,
            ) {
                if (cancellationSignal?.isCanceled == true) {
                    callback.onLayoutCancelled()
                    return
                }
                val info = PrintDocumentInfo.Builder("receipt.pdf")
                    .setContentType(PrintDocumentInfo.CONTENT_TYPE_DOCUMENT)
                    .build()
                callback.onLayoutFinished(info, true)
            }

            override fun onWrite(
                pages: Array<out PageRange>?,
                destination: ParcelFileDescriptor,
                cancellationSignal: CancellationSignal?,
                callback: PrintDocumentAdapter.WriteResultCallback,
            ) {
                try {
                    FileOutputStream(destination.fileDescriptor).use { it.write(pdfBytes) }
                    callback.onWriteFinished(arrayOf(PageRange.ALL_PAGES))
                } catch (e: IOException) {
                    callback.onWriteFailed(e.message)
                }
            }
        }
        printManager.print("ARFA receipt", adapter, PrintAttributes.Builder().build())
    }

    private fun printHtmlPage(html: String) {
        val view = WebView(this)
        printWebView = view
        view.webViewClient = object : WebViewClient() {
            override fun onPageFinished(page: WebView, url: String) {
                val printManager = getSystemService(Context.PRINT_SERVICE) as PrintManager
                printManager.print("ARFA receipt", page.createPrintDocumentAdapter("receipt"),
                    PrintAttributes.Builder().build())
            }
        }
        view.loadDataWithBaseURL(null, html, "text/html", "utf-8", null)
    }

    // --- App settings: printer, drawer, kiosk, update ---------------------------------------

    private fun printerLabel(): String = when (settings.connection) {
        PrinterSettings.BLUETOOTH -> getString(R.string.printer_bt, settings.printerName ?: settings.bluetoothAddress)
        PrinterSettings.USB -> getString(R.string.printer_usb, settings.printerName ?: "")
        else -> getString(R.string.printer_none)
    }

    fun showAppSettings() {
        val items = arrayOf(
            getString(R.string.setting_printer, printerLabel()),
            getString(R.string.setting_paper, if (settings.columns == 32) "2 inch (58 mm)" else "3 inch (80 mm)"),
            getString(if (settings.kickDrawer) R.string.setting_drawer_on else R.string.setting_drawer_off),
            getString(R.string.setting_test),
            getString(R.string.setting_open_drawer),
            getString(if (settings.kioskEnabled) R.string.setting_kiosk_on else R.string.setting_kiosk_off),
            getString(R.string.setting_update, BuildConfig.VERSION_NAME),
        )
        AlertDialog.Builder(this)
            .setTitle(R.string.settings_title)
            .setItems(items) { _, which ->
                when (which) {
                    0 -> choosePrinterType()
                    1 -> {
                        settings.columns = if (settings.columns == 32) 48 else 32
                        showAppSettings()
                    }
                    2 -> {
                        settings.kickDrawer = !settings.kickDrawer
                        showAppSettings()
                    }
                    3 -> if (settings.hasPrinter) {
                        sendToPrinter(EscPosPrinter.testPage(settings.columns, settings.printerName), null)
                    } else {
                        toast(getString(R.string.printer_choose_first))
                    }
                    4 -> if (settings.hasPrinter) sendToPrinter(EscPosPrinter.DRAWER_KICK, null)
                        else toast(getString(R.string.printer_choose_first))
                    5 -> if (settings.kioskEnabled) askKioskPin(unlock = true) else askKioskPin(unlock = false)
                    6 -> UpdateChecker.check(this, settings, manual = true)
                }
            }
            .setNegativeButton(R.string.close, null)
            .show()
    }

    private fun choosePrinterType() {
        val options = arrayOf(getString(R.string.type_bluetooth), getString(R.string.type_usb), getString(R.string.type_none))
        AlertDialog.Builder(this)
            .setTitle(R.string.choose_printer)
            .setItems(options) { _, which ->
                when (which) {
                    0 -> chooseBluetoothPrinter()
                    1 -> chooseUsbPrinter()
                    else -> {
                        settings.connection = PrinterSettings.CONNECTION_NONE
                        toast(getString(R.string.printer_none))
                    }
                }
            }
            .show()
    }

    @SuppressLint("MissingPermission") // checked just below on Android 12+
    private fun chooseBluetoothPrinter() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED
        ) {
            bluetoothPermission.launch(Manifest.permission.BLUETOOTH_CONNECT)
            return
        }
        val adapter = (getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
        if (adapter == null) {
            toast(getString(R.string.no_bluetooth))
            return
        }
        val paired = adapter.bondedDevices?.toList().orEmpty()
        if (paired.isEmpty()) {
            AlertDialog.Builder(this)
                .setTitle(R.string.no_paired_title)
                .setMessage(R.string.no_paired_body)
                .setPositiveButton(R.string.open_bluetooth_settings) { _, _ ->
                    try {
                        startActivity(Intent(Settings.ACTION_BLUETOOTH_SETTINGS))
                    } catch (_: ActivityNotFoundException) {
                    }
                }
                .setNegativeButton(R.string.close, null)
                .show()
            return
        }
        val names = paired.map { (it.name ?: it.address) + "\n" + it.address }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle(R.string.choose_bt)
            .setItems(names) { _, which ->
                val device = paired[which]
                settings.connection = PrinterSettings.BLUETOOTH
                settings.bluetoothAddress = device.address
                settings.printerName = device.name ?: device.address
                toast(getString(R.string.printer_saved, settings.printerName))
            }
            .setNegativeButton(R.string.close, null)
            .show()
    }

    private fun chooseUsbPrinter() {
        val manager = getSystemService(Context.USB_SERVICE) as UsbManager
        val devices = manager.deviceList.values.filter { EscPosPrinter.bulkOut(it) != null }
        if (devices.isEmpty()) {
            toast(getString(R.string.no_usb))
            return
        }
        val names = devices.map { usbName(it) }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle(R.string.choose_usb)
            .setItems(names) { _, which ->
                val device = devices[which]
                settings.connection = PrinterSettings.USB
                settings.usbVendorId = device.vendorId
                settings.usbProductId = device.productId
                settings.printerName = usbName(device)
                if (!manager.hasPermission(device)) {
                    val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0
                    val intent = Intent(ACTION_USB_PERMISSION).setPackage(packageName)
                    manager.requestPermission(device, PendingIntent.getBroadcast(this, 0, intent, flags))
                } else {
                    toast(getString(R.string.printer_saved, settings.printerName))
                }
            }
            .setNegativeButton(R.string.close, null)
            .show()
    }

    private fun usbName(device: UsbDevice): String {
        val product = try {
            device.productName
        } catch (_: SecurityException) {
            null
        }
        return (product ?: "USB printer") + " (" + Integer.toHexString(device.vendorId) + ":" +
            Integer.toHexString(device.productId) + ")"
    }

    // --- kiosk (lock task) ---------------------------------------------------------------------

    private fun askKioskPin(unlock: Boolean) {
        val input = EditText(this)
        input.inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
        input.hint = getString(R.string.kiosk_pin_hint)
        AlertDialog.Builder(this)
            .setTitle(if (unlock) R.string.kiosk_unlock_title else R.string.kiosk_lock_title)
            .setMessage(if (unlock) R.string.kiosk_unlock_body else R.string.kiosk_lock_body)
            .setView(input)
            .setPositiveButton(R.string.ok) { _, _ ->
                val pin = input.text.toString().trim()
                if (unlock) {
                    if (pin == settings.kioskPin) {
                        settings.kioskEnabled = false
                        stopKiosk()
                    } else {
                        toast(getString(R.string.kiosk_wrong_pin))
                    }
                } else if (pin.length < 4) {
                    toast(getString(R.string.kiosk_pin_short))
                } else {
                    settings.kioskPin = pin
                    settings.kioskEnabled = true
                    startKiosk()
                }
            }
            .setNegativeButton(R.string.close, null)
            .show()
    }

    private fun startKiosk() {
        val manager = getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        if (manager.lockTaskModeState != ActivityManager.LOCK_TASK_MODE_NONE) return
        try {
            // A device set up as a dedicated device (device owner) locks
            // straight away; any other phone asks to pin the screen.
            startLockTask()
        } catch (e: IllegalStateException) {
            toast(getString(R.string.kiosk_failed))
        } catch (e: SecurityException) {
            toast(getString(R.string.kiosk_failed))
        }
    }

    private fun stopKiosk() {
        try {
            stopLockTask()
        } catch (_: Exception) {
        }
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    companion object {
        private const val ACTION_USB_PERMISSION = "com.arfa.pos.USB_PERMISSION"
    }
}

/** Links the WebView hands to the phone instead of opening itself. */
private val SHARE_HOSTS = setOf("wa.me", "api.whatsapp.com")
