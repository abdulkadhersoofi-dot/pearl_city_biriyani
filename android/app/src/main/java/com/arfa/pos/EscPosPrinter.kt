package com.arfa.pos

import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.content.Context
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import java.io.IOException
import java.util.UUID

/**
 * Sends raw ESC/POS bytes to the counter's thermal printer - over
 * Bluetooth (the Serial Port Profile every Bluetooth receipt printer
 * speaks) or USB (the printer's bulk OUT endpoint). The receipt itself is
 * laid out by the web page (app/static/js/escpos.js); this class only
 * moves bytes. Blocking: always call it off the main thread.
 */
class EscPosPrinter(private val context: Context, private val settings: PrinterSettings) {

    @Throws(IOException::class)
    fun send(bytes: ByteArray) {
        when (settings.connection) {
            PrinterSettings.BLUETOOTH -> sendBluetooth(bytes)
            PrinterSettings.USB -> sendUsb(bytes)
            else -> throw IOException("No receipt printer is set up.")
        }
    }

    // Bluetooth permission (BLUETOOTH_CONNECT on Android 12+) is asked for
    // when the printer is chosen - see MainActivity.chooseBluetoothPrinter.
    @SuppressLint("MissingPermission")
    private fun sendBluetooth(bytes: ByteArray) {
        val manager = context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        val adapter = manager.adapter ?: throw IOException("This device has no Bluetooth.")
        if (!adapter.isEnabled) throw IOException("Bluetooth is off - turn it on and print again.")
        val address = settings.bluetoothAddress ?: throw IOException("No Bluetooth printer is chosen.")
        val socket = try {
            adapter.getRemoteDevice(address).createRfcommSocketToServiceRecord(SPP_UUID)
        } catch (e: SecurityException) {
            throw IOException("Allow Nearby devices for ARFA POS, then print again.")
        } catch (e: IllegalArgumentException) {
            throw IOException("The saved Bluetooth printer address isn't valid - choose the printer again.")
        }
        try {
            socket.connect()
            val out = socket.outputStream
            out.write(bytes)
            out.flush()
            // Let the printer take the last bytes before the link drops.
            Thread.sleep(500)
        } catch (e: SecurityException) {
            throw IOException("Allow Nearby devices for ARFA POS, then print again.")
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        } finally {
            try {
                socket.close()
            } catch (_: IOException) {
            }
        }
    }

    private fun sendUsb(bytes: ByteArray) {
        val manager = context.getSystemService(Context.USB_SERVICE) as UsbManager
        val device = findUsbDevice(manager) ?: throw IOException("The USB printer isn't connected.")
        if (!manager.hasPermission(device)) {
            throw IOException("Allow ARFA POS to use the USB printer - open Printer settings and choose it again.")
        }
        val target = bulkOut(device) ?: throw IOException("That USB device doesn't take print data.")
        val connection = manager.openDevice(device) ?: throw IOException("Couldn't open the USB printer.")
        try {
            if (!connection.claimInterface(target.first, true)) throw IOException("The USB printer is busy.")
            var offset = 0
            while (offset < bytes.size) {
                val chunk = bytes.copyOfRange(offset, minOf(offset + CHUNK, bytes.size))
                val sent = connection.bulkTransfer(target.second, chunk, chunk.size, 5000)
                if (sent <= 0) throw IOException("The USB printer stopped responding.")
                offset += sent
            }
            connection.releaseInterface(target.first)
        } finally {
            connection.close()
        }
    }

    fun findUsbDevice(manager: UsbManager): UsbDevice? =
        manager.deviceList.values.firstOrNull {
            it.vendorId == settings.usbVendorId && it.productId == settings.usbProductId
        }

    companion object {
        /** Bluetooth Serial Port Profile - what receipt printers listen on. */
        val SPP_UUID: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
        private const val CHUNK = 4096

        /** ESC p 0 25 250 - pulse the cash drawer wired to the printer (pin 2). */
        val DRAWER_KICK = byteArrayOf(0x1B, 0x70, 0x00, 0x19, 0xFA.toByte())

        fun bulkOut(device: UsbDevice): Pair<UsbInterface, UsbEndpoint>? {
            for (i in 0 until device.interfaceCount) {
                val iface = device.getInterface(i)
                for (e in 0 until iface.endpointCount) {
                    val endpoint = iface.getEndpoint(e)
                    if (endpoint.type == UsbConstants.USB_ENDPOINT_XFER_BULK &&
                        endpoint.direction == UsbConstants.USB_DIR_OUT
                    ) {
                        return Pair(iface, endpoint)
                    }
                }
            }
            return null
        }

        /** A short test receipt in plain ESC/POS. */
        fun testPage(columns: Int, printerName: String?): ByteArray {
            val rule = "-".repeat(columns)
            val text = buildString {
                append("ARFA POS\n")
                append("Printer test\n")
                append(rule).append('\n')
                append("Printer: ").append(printerName ?: "-").append('\n')
                append("Paper: ").append(if (columns == 32) "2 inch (58 mm)" else "3 inch (80 mm)").append('\n')
                append("1234567890".repeat(columns / 10 + 1).take(columns)).append('\n')
                append(rule).append('\n')
                append("If this prints cleanly, receipts will too.\n")
            }
            val out = java.io.ByteArrayOutputStream()
            out.write(byteArrayOf(0x1B, 0x40)) // initialise
            out.write(byteArrayOf(0x1B, 0x61, 0x01)) // centre
            out.write(text.toByteArray(Charsets.US_ASCII))
            out.write(byteArrayOf(0x1B, 0x64, 0x04)) // feed 4 lines
            out.write(byteArrayOf(0x1D, 0x56, 0x42, 0x00)) // cut
            return out.toByteArray()
        }
    }
}
