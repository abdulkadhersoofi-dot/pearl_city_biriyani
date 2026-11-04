package com.arfa.scan

import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** Updates come from the ARFA server: /api/scan/v1/app-update names the
 * newest ARFA Scan there and hands a day-long download link (app/apps on
 * the server). Only while the server has none published yet does the app
 * look at the GitHub "android-latest" release, as older builds did. A
 * newer build is offered at most twice a day, or on demand from the menu. */
object UpdateChecker {
    private const val INTERVAL_MS = 12 * 60 * 60 * 1000L

    private fun latest(file: String) =
        "https://github.com/${BuildConfig.RELEASE_REPO}/releases/download/android-latest/$file"

    /** The server's answer when it has a build, else GitHub's. */
    private fun newest(): JSONObject {
        val site = try { Api.get("/app-update") } catch (e: Exception) { null }
        if (site != null && site.optBoolean("available") && site.optInt("versionCode", 0) > 0) return site
        val connection = URL(latest("scan-version.json")).openConnection() as HttpURLConnection
        connection.connectTimeout = 10000
        connection.readTimeout = 10000
        return JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
    }

    fun check(activity: AppCompatActivity, manual: Boolean) {
        val now = System.currentTimeMillis()
        if (!manual && now - Api.session.lastUpdateCheck < INTERVAL_MS) return
        Thread {
            try {
                val json = newest()
                Api.session.lastUpdateCheck = now
                val code = json.optInt("versionCode", 0)
                val name = json.optString("versionName", code.toString())
                val apk = json.optString("apk", latest("ARFA-Scan.apk"))
                activity.runOnUiThread {
                    if (activity.isFinishing) return@runOnUiThread
                    if (code > BuildConfig.VERSION_CODE) {
                        AlertDialog.Builder(activity)
                            .setTitle(R.string.update_title)
                            .setMessage(activity.getString(R.string.update_body, name, BuildConfig.VERSION_NAME))
                            .setPositiveButton(R.string.update_download) { _, _ ->
                                try {
                                    activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(apk)))
                                } catch (e: ActivityNotFoundException) {
                                    Toast.makeText(activity, R.string.update_failed, Toast.LENGTH_LONG).show()
                                }
                            }
                            .setNegativeButton(R.string.later, null)
                            .show()
                    } else if (manual) {
                        Toast.makeText(activity, activity.getString(R.string.update_none, BuildConfig.VERSION_NAME),
                            Toast.LENGTH_LONG).show()
                    }
                }
            } catch (e: Exception) {
                if (manual) activity.runOnUiThread {
                    Toast.makeText(activity, R.string.update_failed, Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }
}
