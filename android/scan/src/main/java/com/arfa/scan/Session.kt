package com.arfa.scan

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.json.JSONObject

/** Who is signed in on this phone: the server's address, the sign-in token
 * (kept encrypted by the Android keystore) and the last "me" the server
 * gave - so the app opens at once, before the first answer arrives. */
class Session(context: Context) {

    private val prefs: SharedPreferences = try {
        val key = MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build()
        EncryptedSharedPreferences.create(
            context, "arfa_scan_session", key,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    } catch (e: Exception) {
        // A keystore that refuses (rare, some old phones): plain private storage.
        context.getSharedPreferences("arfa_scan_session_plain", Context.MODE_PRIVATE)
    }

    var token: String?
        get() = prefs.getString("token", null)
        set(value) = prefs.edit().putString("token", value).apply()

    var server: String
        get() = prefs.getString("server", null)?.takeIf { it.startsWith("https://") } ?: BuildConfig.SERVER_BASE_URL
        set(value) = prefs.edit().putString("server", value.trim().trimEnd('/')).apply()

    var email: String
        get() = prefs.getString("email", "") ?: ""
        set(value) = prefs.edit().putString("email", value).apply()

    var me: JSONObject?
        get() = prefs.getString("me", null)?.let { runCatching { JSONObject(it) }.getOrNull() }
        set(value) = prefs.edit().putString("me", value?.toString()).apply()

    var lastUpdateCheck: Long
        get() = prefs.getLong("last_update_check", 0L)
        set(value) = prefs.edit().putLong("last_update_check", value).apply()

    val signedIn: Boolean get() = !token.isNullOrEmpty()

    fun signOut() {
        prefs.edit().remove("token").remove("me").apply()
    }
}
