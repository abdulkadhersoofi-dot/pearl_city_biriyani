package com.arfa.scan

import android.content.Context
import android.net.Uri
import android.util.Base64
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import kotlinx.coroutines.tasks.await
import org.json.JSONObject

/** Reads a bill on the phone itself, before it is sent: the printed text
 * (Google's on-device text recognition) and the e-invoice QR. Both travel
 * with the picture - the server's reader uses the text as a second pair of
 * eyes, and the QR when the picture's copy of it can't be decoded. */
object PhoneReader {

    data class Read(val text: String, val qr: String?)

    private val GSTIN = Regex("\\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]\\b")

    suspend fun read(context: Context, pages: List<Uri>): Read {
        val recognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
        val scanner = BarcodeScanning.getClient(
            BarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
        )
        val texts = mutableListOf<String>()
        var qr: String? = null
        try {
            for (uri in pages.take(4)) {
                val image = runCatching { InputImage.fromFilePath(context, uri) }.getOrNull() ?: continue
                runCatching { recognizer.process(image).await().text }.getOrNull()?.let { texts += it }
                if (qr == null) {
                    qr = runCatching { scanner.process(image).await() }.getOrNull()
                        ?.mapNotNull { it.rawValue }
                        ?.firstOrNull { it.count { c -> c == '.' } >= 2 && it.length > 100 }
                }
            }
        } finally {
            recognizer.close()
            scanner.close()
        }
        return Read(texts.joinToString("\n--- page ---\n").take(20000), qr)
    }

    /** A line for the bubble: what the phone could tell on its own. */
    fun summary(read: Read): String {
        val bits = mutableListOf<String>()
        val qr = read.qr?.let { qrFields(it) }
        if (qr != null) {
            bits += "e-Invoice QR found"
            qr.optString("DocNo").takeIf { it.isNotEmpty() }?.let { bits += "Inv $it" }
            qr.optDouble("TotInvVal").takeIf { !it.isNaN() && it > 0 }?.let { bits += "₹" + Format.money(it) }
        } else {
            GSTIN.find(read.text.uppercase())?.let { bits += "GSTIN ${it.value}" }
        }
        return bits.joinToString(" · ")
    }

    /** The e-invoice QR is a signed token; its middle part holds the bill. */
    fun qrFields(raw: String): JSONObject? = runCatching {
        val payload = String(Base64.decode(raw.split(".")[1], Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING))
        val json = JSONObject(payload)
        val data = json.opt("data")
        when (data) {
            is JSONObject -> data
            is String -> JSONObject(data)
            else -> null
        }
    }.getOrNull()
}
