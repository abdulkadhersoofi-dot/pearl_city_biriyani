package com.arfa.pos

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.net.http.SslError
import android.os.Bundle
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.print.PageRange
import android.print.PrintAttributes
import android.print.PrintDocumentAdapter
import android.print.PrintDocumentInfo
import android.print.PrintManager
import android.view.View
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.addCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import com.arfa.pos.databinding.ActivityMainBinding
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Single-screen shell: a full-screen WebView locked to this app's own
 * ARFA server, for branch (POS) logins only. Everything a cashier does
 * - sign in, bill, hold/resume, Day-book, Z-report - is the same
 * server-rendered UI as the website; this activity's job is just to be
 * a clean native frame around it (splash, status bar, offline screen,
 * back button) and to enforce "branch logins only" since the server
 * itself serves every role from the same /auth/login.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val baseUrl: String = BuildConfig.SERVER_BASE_URL
    private val baseHost: String = Uri.parse(baseUrl).host ?: ""

    // Paths a branch (Staff) login is allowed to be on. Everything else
    // reachable only by another role (Invoices, Reports, Settings, the
    // admin/auditor areas, ...) lands here only if the account that
    // just signed in isn't actually a branch login - see
    // handlePageFinished().
    private val allowedPrefixes = listOf("/auth/", "/pos/", "/media/", "/static/", "/healthz")

    private var isHandlingBlockedRole = false
    private var hasLoadedOnce = false
    private var backPressedAt = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        val splash = installSplashScreen()
        super.onCreate(savedInstanceState)
        splash.setKeepOnScreenCondition { !hasLoadedOnce }

        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setupWebView()
        binding.retryButton.setOnClickListener { reload() }

        onBackPressedDispatcher.addCallback(this) {
            when {
                binding.webView.canGoBack() -> binding.webView.goBack()
                isAtLoginPage() -> confirmExitOnDoubleBack()
                else -> binding.webView.loadUrl("$baseUrl/auth/login")
            }
        }

        if (savedInstanceState == null) {
            binding.webView.loadUrl("$baseUrl/auth/login")
        }
    }

    private fun isAtLoginPage(): Boolean {
        val path = Uri.parse(binding.webView.url ?: "").path ?: ""
        return path.startsWith("/auth/login") || path.isEmpty()
    }

    private fun confirmExitOnDoubleBack() {
        val now = System.currentTimeMillis()
        if (now - backPressedAt < 2000) {
            finish()
        } else {
            backPressedAt = now
            Toast.makeText(this, getString(R.string.exit_confirm), Toast.LENGTH_SHORT).show()
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val webView = binding.webView
        val settings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.useWideViewPort = true
        settings.loadWithOverviewMode = true
        settings.cacheMode = android.webkit.WebSettings.LOAD_DEFAULT

        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false)

        webView.addJavascriptInterface(PrintBridge(), "AndroidPrint")
        webView.webViewClient = AppWebViewClient()
    }

    private fun reload() {
        binding.errorView.visibility = View.GONE
        binding.loadingSpinner.visibility = View.VISIBLE
        val current = binding.webView.url
        if (current.isNullOrBlank()) {
            binding.webView.loadUrl("$baseUrl/auth/login")
        } else {
            binding.webView.reload()
        }
    }

    private fun showError() {
        binding.webView.visibility = View.INVISIBLE
        binding.loadingSpinner.visibility = View.GONE
        binding.errorView.visibility = View.VISIBLE
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
                    Toast.makeText(this@MainActivity, R.string.no_app_to_share, Toast.LENGTH_LONG).show()
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
     * itself is public.
     */
    inner class PrintBridge {
        @JavascriptInterface
        fun printReceipt(url: String) {
            val absoluteUrl = if (url.startsWith("http")) url else baseUrl + url
            runOnUiThread { downloadAndPrint(absoluteUrl) }
        }
    }

    /**
     * The web app prints a receipt by loading a server-rendered PDF
     * into a hidden iframe and calling window.print() on it - desktop
     * browsers handle that natively, but Android's WebView has no
     * built-in PDF viewer or print dialog to drive that way. Instead,
     * this downloads the same PDF (reusing the WebView's own session
     * cookie) and hands the raw bytes to Android's PrintManager, which
     * shows the real system print dialog - works with whatever printer
     * (thermal receipt printer included) the device has a print
     * service configured for.
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
                runOnUiThread {
                    Toast.makeText(this, "Could not load the receipt. Check your connection.", Toast.LENGTH_LONG).show()
                }
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
}

/** Links the WebView hands to the phone instead of opening itself. */
private val SHARE_HOSTS = setOf("wa.me", "api.whatsapp.com")
