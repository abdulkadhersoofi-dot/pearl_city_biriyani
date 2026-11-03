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

/** The build publishes ARFA-Scan.apk and scan-version.json on the
 * "android-latest" release (.github/workflows/android-apk.yml); a newer
 * build is offered at most twice a day, or on demand from the menu. */
object UpdateChecker {
    private const val INTERVAL_MS = 12 * 60 * 60 * 1000L

    private fun latest(file: String) =
        "https://github.com/${BuildConfig.RELEASE_REPO}/releases/download/android-latest/$file"

    fun check(activity: AppCompatActivity, manual: Boolean) {
        val now = System.currentTimeMillis()
        if (!manual && now - Api.session.lastUpdateCheck < INTERVAL_MS) return
        Thread {
            try {
                val connection = URL(latest("scan-version.json")).openConnection() as HttpURLConnection
                connection.connectTimeout = 10000
                connection.readTimeout = 10000
                val json = JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
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
