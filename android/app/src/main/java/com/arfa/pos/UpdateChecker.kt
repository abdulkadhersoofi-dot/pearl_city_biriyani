package com.arfa.pos

import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * In-app update check: the GitHub Actions build publishes the APK and a
 * small version.json ({"versionCode": N, "versionName": "...", "apk": url})
 * on the "android-latest" release (.github/workflows/android-apk.yml).
 * When that versionCode is newer than this build's, the app offers the
 * download; Android's own installer does the rest. Checked at most twice
 * a day on start, or on demand from App settings.
 */
object UpdateChecker {

    private const val INTERVAL_MS = 12 * 60 * 60 * 1000L

    fun latestUrl(file: String): String =
        "https://github.com/${BuildConfig.RELEASE_REPO}/releases/download/android-latest/$file"

    fun check(activity: MainActivity, settings: PrinterSettings, manual: Boolean) {
        val now = System.currentTimeMillis()
        if (!manual && now - settings.lastUpdateCheck < INTERVAL_MS) return
        Thread {
            try {
                val connection = URL(latestUrl("version.json")).openConnection() as HttpURLConnection
                connection.connectTimeout = 10000
                connection.readTimeout = 10000
                connection.instanceFollowRedirects = true
                if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                    throw IOException("HTTP ${connection.responseCode}")
                }
                val body = connection.inputStream.bufferedReader().use { it.readText() }
                val json = JSONObject(body)
                settings.lastUpdateCheck = now
                val latest = json.optInt("versionCode", 0)
                val name = json.optString("versionName", latest.toString())
                val apk = json.optString("apk", latestUrl("ARFA-Counter.apk"))
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

    private fun open(activity: MainActivity, url: String) {
        try {
            activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (e: ActivityNotFoundException) {
            Toast.makeText(activity, R.string.update_failed, Toast.LENGTH_LONG).show()
        }
    }
}
