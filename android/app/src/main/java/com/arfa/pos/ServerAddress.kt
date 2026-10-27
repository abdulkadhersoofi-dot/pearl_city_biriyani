package com.arfa.pos

import android.content.Context
import android.content.SharedPreferences

/**
 * Which server this phone talks to. It starts as the address built into
 * the app (BuildConfig.SERVER_BASE_URL); the update check can name a new
 * one ("server_url" in the release's version.json, from android/server.json
 * in the repository), so moving the server to a new address is a setting,
 * not a reinstall on every phone.
 *
 * A web page's storage belongs to its address, and the phone's pairing,
 * what it is for (Counter / Waiter / Kitchen), its held bills and waiting
 * KOTs live there. So the move is made by MainActivity in two steps: on the
 * old address it copies everything the app keeps ("arfa.*" in
 * localStorage) - only once no offline bill is waiting on the phone - and
 * on the new address it writes the copy back before the app's screens open.
 */
class ServerAddress(private val context: Context) {

    companion object {
        /** The address built into every version before 1.9 (Render). */
        const val EARLIER_BUILT_IN = "https://gst-billing-app-prdx.onrender.com"
    }

    private val prefs: SharedPreferences = context.getSharedPreferences("arfa_server", Context.MODE_PRIVATE)

    /** The address in use - written down the first time it is read, so a
     * later change of the built-in address never moves a phone by itself. */
    var current: String
        get() = prefs.getString("current", null) ?: startingAddress().also { prefs.edit().putString("current", it).apply() }
        set(value) = prefs.edit().putString("current", clean(value)).apply()

    /**
     * Where a phone starts. A new install: the address built in. An older
     * version updated to this one never wrote its address down - it was
     * using the one built into that version, and its pairing and any bill
     * still waiting to be sent are there. It stays there and moves the
     * careful way (offer() below, then MainActivity), never by a jump.
     */
    private fun startingAddress(): String {
        val updated = try {
            val info = context.packageManager.getPackageInfo(context.packageName, 0)
            info.lastUpdateTime > info.firstInstallTime
        } catch (e: Exception) {
            false
        }
        return if (updated) EARLIER_BUILT_IN else BuildConfig.SERVER_BASE_URL
    }

    /** A new address named by the update check, not moved to yet. */
    var pending: String?
        get() = prefs.getString("pending", null)
        set(value) = prefs.edit().putString("pending", value?.let(::clean)).apply()

    /** What the app kept on the old address, waiting to be written on the new one (JSON). */
    var carried: String?
        get() = prefs.getString("carried", null)
        set(value) = prefs.edit().putString("carried", value).apply()

    /** Called with "server_url" from version.json: only a real https address that differs counts. */
    fun offer(url: String?) {
        val next = clean(url ?: return)
        if (!next.startsWith("https://") || next.length < 12 || next == current) return
        pending = next
    }

    private fun clean(url: String): String = url.trim().trimEnd('/')
}
