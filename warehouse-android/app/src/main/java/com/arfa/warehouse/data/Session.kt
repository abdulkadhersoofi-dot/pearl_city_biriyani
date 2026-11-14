package com.arfa.warehouse.data

import android.content.Context
import android.content.SharedPreferences
import android.os.Build
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.arfa.warehouse.BuildConfig
import org.json.JSONArray
import org.json.JSONObject

/** This phone's sign-in, kept encrypted by the Android keystore: the refresh
 * token (sign-in, once per phone), the access token (one PIN unlock), the
 * last "me" and the chosen warehouse - plus the phone's own settings. */
class Session(context: Context) {

    private val prefs: SharedPreferences = try {
        val key = MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build()
        EncryptedSharedPreferences.create(
            context, "arfa_warehouse_session", key,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    } catch (e: Exception) {
        context.getSharedPreferences("arfa_warehouse_session_plain", Context.MODE_PRIVATE)
    }

    val server: String get() = BuildConfig.SERVER_BASE_URL

    var refreshToken: String?
        get() = prefs.getString("refresh", null)
        set(value) = prefs.edit().putString("refresh", value).apply()

    var accessToken: String?
        get() = prefs.getString("access", null)
        set(value) = prefs.edit().putString("access", value).apply()

    var businessCode: String
        get() = prefs.getString("business_code", "") ?: ""
        set(value) = prefs.edit().putString("business_code", value).apply()

    var loginId: String
        get() = prefs.getString("login_id", "") ?: ""
        set(value) = prefs.edit().putString("login_id", value).apply()

    var me: JSONObject?
        get() = prefs.getString("me", null)?.let { runCatching { JSONObject(it) }.getOrNull() }
        set(value) = prefs.edit().putString("me", value?.toString()).apply()

    var warehouseId: Long
        get() = prefs.getLong("warehouse_id", 0L)
        set(value) = prefs.edit().putLong("warehouse_id", value).apply()

    var pushConfig: JSONObject?
        get() = prefs.getString("push", null)?.let { runCatching { JSONObject(it) }.getOrNull() }
        set(value) = prefs.edit().putString("push", value?.toString()).apply()

    var fcmToken: String?
        get() = prefs.getString("fcm_token", null)
        set(value) = prefs.edit().putString("fcm_token", value).apply()

    var fcmTokenSent: String?
        get() = prefs.getString("fcm_token_sent", null)
        set(value) = prefs.edit().putString("fcm_token_sent", value).apply()

    var deviceName: String
        get() = prefs.getString("device_name", null) ?: "${Build.MANUFACTURER} ${Build.MODEL}".trim()
        set(value) = prefs.edit().putString("device_name", value.take(120)).apply()

    var sound: Boolean
        get() = prefs.getBoolean("sound", true)
        set(value) = prefs.edit().putBoolean("sound", value).apply()

    /** Rugged scanner phones (Zebra / Honeywell intents, keyboard wedge). */
    var hardwareScanner: Boolean
        get() = prefs.getBoolean("hardware_scanner", false)
        set(value) = prefs.edit().putBoolean("hardware_scanner", value).apply()

    var lastActive: Long
        get() = prefs.getLong("last_active", 0L)
        set(value) = prefs.edit().putLong("last_active", value).apply()

    var lastUpdateCheck: Long
        get() = prefs.getLong("last_update_check", 0L)
        set(value) = prefs.edit().putLong("last_update_check", value).apply()

    private var pickCounter: Long
        get() = prefs.getLong("pick_counter", 0L)
        set(value) = prefs.edit().putLong("pick_counter", value).apply()

    /** A client id no other scan from this phone has used (the server keeps
     * a scan sent twice as one). */
    @Synchronized
    fun nextClientId(prefix: String): String {
        val n = pickCounter + 1
        pickCounter = n
        val phone = (prefs.getString("phone_id", null) ?: java.util.UUID.randomUUID().toString().take(8).also {
            prefs.edit().putString("phone_id", it).apply()
        })
        return "$prefix-$phone-$n"
    }

    val signedIn: Boolean get() = !refreshToken.isNullOrEmpty()
    val pinSet: Boolean get() = me?.optBoolean("pin_set") == true
    val loginName: String get() = me?.optJSONObject("login")?.optString("name") ?: ""
    val manualSerial: Boolean get() = me?.optJSONObject("login")?.optBoolean("manual_serial") == true
    val idleLockMinutes: Int get() = me?.optInt("idle_lock_minutes", 5) ?: 5
    val pollSeconds: Int get() = me?.optInt("poll_seconds", 30) ?: 30

    fun warehouses(): List<Pair<Long, String>> {
        val list = me?.optJSONArray("warehouses") ?: JSONArray()
        return (0 until list.length()).map { list.getJSONObject(it) }.map { it.getLong("id") to it.getString("name") }
    }

    fun warehouseName(): String = warehouses().firstOrNull { it.first == warehouseId }?.second ?: ""

    /** The chosen warehouse, or the only one. */
    fun ensureWarehouse() {
        val all = warehouses()
        if (all.none { it.first == warehouseId }) warehouseId = all.firstOrNull()?.first ?: 0L
    }

    fun signOut() {
        prefs.edit().remove("refresh").remove("access").remove("me").remove("fcm_token_sent").apply()
    }
}
