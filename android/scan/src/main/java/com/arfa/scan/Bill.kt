package com.arfa.scan

import org.json.JSONObject

/** One scanned bill as the server describes it (api.py _bubble). */
data class Bill(
    val id: Int,
    val status: String,
    val state: String,          // reading / passed / look / duplicate / retake / posted / filed / discarded
    val supplier: String,
    val gstin: String,
    val invoiceNumber: String,
    val invoiceDate: String,
    val total: Double?,
    val issues: Int,
    val issue: String,
    val message: String,
    val duplicateOf: String,
    val filename: String,
    val uploadedAt: String,
    val readAt: String,
    val from: String,
    val qr: Boolean,
    val canApprove: Boolean,
    val thumb: String,
) {
    /** What a search is matched against: names, numbers, and amounts
     * with or without their commas. */
    val searchText: String by lazy {
        val amount = total?.let { Format.money(it) } ?: ""
        listOf(supplier, gstin, invoiceNumber, invoiceDate, filename, from, amount, amount.replace(",", ""),
            total?.toString() ?: "").joinToString(" ").lowercase()
    }

    companion object {
        fun from(j: JSONObject) = Bill(
            id = j.optInt("id"), status = j.optString("status"), state = j.optString("state"),
            supplier = j.optString("supplier"), gstin = j.optString("gstin"),
            invoiceNumber = j.optString("invoice_number"), invoiceDate = j.optString("invoice_date"),
            total = if (j.isNull("total")) null else j.optDouble("total").takeIf { !it.isNaN() },
            issues = j.optInt("issues"), issue = j.optString("issue"), message = j.optString("message"),
            duplicateOf = j.optString("duplicate_of"), filename = j.optString("filename"),
            uploadedAt = j.optString("uploaded_at"), readAt = j.optString("read_at"),
            from = j.optString("from"), qr = j.optBoolean("qr"), canApprove = j.optBoolean("can_approve"),
            thumb = j.optString("thumb"),
        )
    }
}
