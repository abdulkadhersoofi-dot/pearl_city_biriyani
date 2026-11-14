package com.arfa.pos

import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.webkit.CookieManager
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * In-app update check. The ARFA server answers /apps/counter/latest.json
 * ({"versionCode": N, "versionName": "...", "apk": url}) for the phone's
 * signed-in session (the WebView's own cookie), with a day-long download
 * link (app/apps on the server). Only when the server can't answer, or has
 * no build published yet, does the app read the version.json on GitHub's
 * "android-latest" release (.github/workflows/android-apk.yml), as older
 * builds did - which is also how a phone hears that the server has moved
 * (server_url). When the versionCode is newer than this build's, the app
 * offers the download; Android's own installer does the rest. Checked at
 * most twice a day on start, or on demand from App settings.
 */
object UpdateChecker {

    private const val INTERVAL_MS = 12 * 60 * 60 * 1000L

    fun latestUrl(file: String): String =
        "https://github.com/${BuildConfig.RELEASE_REPO}/releases/download/android-latest/$file"

    fun check(activity: MainActivity, settings: PrinterSettings, server: ServerAddress, manual: Boolean) {
        val now = System.currentTimeMillis()
        if (!manual && now - settings.lastUpdateCheck < INTERVAL_MS) return
        val base = server.current
        // Read here, on the main thread: the WebView's signed-in session.
        val cookie = try { CookieManager.getInstance().getCookie(base) } catch (e: Exception) { null }
        Thread {
            try {
                val site = try { fetch("$base/apps/counter/latest.json", cookie, redirects = false) } catch (e: Exception) { null }
                val json = if (site != null && site.optBoolean("available") && site.optInt("versionCode", 0) > 0) site
                           else fetch(latestUrl("version.json"), null, redirects = true)
                settings.lastUpdateCheck = now
                val latest = json.optInt("versionCode", 0)
                val name = json.optString("versionName", latest.toString())
                val apk = json.optString("apk", latestUrl("ARFA-Counter.apk"))
                // The server's address, when it has moved (android/server.json).
                server.offer(json.optString("server_url", ""))
                activity.runOnUiThread {
                    if (activity.isFinishing) return@runOnUiThread
                    if (latest > BuildConfig.VERSION_CODE) {
                        AlertDialog.Builder(activity)
                            .setTitle(R.string.update_title)
                            .setMessage(activity.getString(R.string.update_body, name, BuildConfig.VERSION_NAME))
                            .setPositiveButton(R.string.update_download) { _, _ -> open(activity, apk) }
                            .setNegativeButton(R.string.later, null)
                            .show()
                    } else if (manual) {
                        Toast.makeText(activity, activity.getString(R.string.update_none, BuildConfig.VERSION_NAME),
                            Toast.LENGTH_LONG).show()
                    }
                }
            } catch (e: Exception) {
                if (manual) {
                    activity.runOnUiThread {
                        Toast.makeText(activity, R.string.update_failed, Toast.LENGTH_LONG).show()
                    }
                }
            }
        }.start()
    }

    private fun fetch(url: String, cookie: String?, redirects: Boolean): JSONObject {
        val connection = URL(url).openConnection() as HttpURLConnection
        connection.connectTimeout = 10000
        connection.readTimeout = 10000
        // The server's own check never follows a redirect: a signed-out
        // session would land on the sign-in page, not on JSON.
        connection.instanceFollowRedirects = redirects
        connection.setRequestProperty("Accept", "application/json")
        if (!cookie.isNullOrEmpty()) connection.setRequestProperty("Cookie", cookie)
        try {
            if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                throw IOException("HTTP ${connection.responseCode}")
            }
            return JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
        } finally {
            connection.disconnect()
        }
    }

    private fun open(activity: MainActivity, url: String) {
        try {
            activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (e: ActivityNotFoundException) {
            Toast.makeText(activity, R.string.update_failed, Toast.LENGTH_LONG).show()
        }
    }
}
