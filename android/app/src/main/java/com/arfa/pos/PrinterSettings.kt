package com.arfa.pos

import android.content.Context
import android.content.SharedPreferences

/**
 * What this phone/tablet is set up with - the thermal printer it prints
 * receipts to, the paper width, the cash drawer and the kiosk lock. Kept
 * on the device (SharedPreferences): each counter has its own printer.
 */
class PrinterSettings(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("arfa_printer", Context.MODE_PRIVATE)

    /** [CONNECTION_NONE] (Android's print dialog, as before), [BLUETOOTH] or [USB]. */
    var connection: String
        get() = prefs.getString("connection", CONNECTION_NONE) ?: CONNECTION_NONE
        set(value) = prefs.edit().putString("connection", value).apply()

    var bluetoothAddress: String?
        get() = prefs.getString("bt_address", null)
        set(value) = prefs.edit().putString("bt_address", value).apply()

    var printerName: String?
        get() = prefs.getString("printer_name", null)
        set(value) = prefs.edit().putString("printer_name", value).apply()

    var usbVendorId: Int
        get() = prefs.getInt("usb_vendor", 0)
        set(value) = prefs.edit().putInt("usb_vendor", value).apply()

    var usbProductId: Int
        get() = prefs.getInt("usb_product", 0)
        set(value) = prefs.edit().putInt("usb_product", value).apply()

    /** Characters per line: 32 for a 2-inch (58 mm) roll, 48 for 3-inch (80 mm). */
    var columns: Int
        get() = prefs.getInt("columns", 48)
        set(value) = prefs.edit().putInt("columns", if (value == 32) 32 else 48).apply()

    /** Kick the cash drawer (wired to the printer) when a cash sale prints. */
    var kickDrawer: Boolean
        get() = prefs.getBoolean("kick_drawer", true)
        set(value) = prefs.edit().putBoolean("kick_drawer", value).apply()

    var kioskEnabled: Boolean
        get() = prefs.getBoolean("kiosk", false)
        set(value) = prefs.edit().putBoolean("kiosk", value).apply()

    /** The code that unlocks kiosk mode again, set when it was turned on. */
    var kioskPin: String?
        get() = prefs.getString("kiosk_pin", null)
        set(value) = prefs.edit().putString("kiosk_pin", value).apply()

    var lastUpdateCheck: Long
        get() = prefs.getLong("last_update_check", 0L)
        set(value) = prefs.edit().putLong("last_update_check", value).apply()

    val hasPrinter: Boolean
        get() = when (connection) {
            BLUETOOTH -> !bluetoothAddress.isNullOrBlank()
            USB -> usbVendorId != 0
            else -> false
        }

    companion object {
        const val CONNECTION_NONE = "none"
        const val BLUETOOTH = "bluetooth"
        const val USB = "usb"
    }
}
