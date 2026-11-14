package com.arfa.warehouse.data

import org.json.JSONArray
import org.json.JSONObject

private fun JSONArray?.objects(): List<JSONObject> =
    if (this == null) emptyList() else (0 until length()).map { getJSONObject(it) }

private fun JSONArray?.strings(): List<String> =
    if (this == null) emptyList() else (0 until length()).map { getString(it) }

private fun JSONObject.str(key: String): String = if (isNull(key)) "" else optString(key, "")

data class OrderCard(
    val id: Long, val number: String, val status: String, val cancelled: Boolean, val branch: String,
    val salesperson: String, val customer: String, val items: List<Pair<String, Int>>, val units: Int,
    val picked: Int, val ageMinutes: Int, val claimedBy: String?, val mine: Boolean,
    // EMI sale: pinned in red with "customer waiting"; Confirm reads "Lock for EMI".
    val emi: Boolean = false, val financier: String? = null, val invoice: String? = null,
    val toDeliver: Boolean = false,
) {
    companion object {
        fun from(j: JSONObject) = OrderCard(
            id = j.getLong("id"), number = j.str("number"), status = j.str("status"),
            cancelled = j.optBoolean("cancelled"), branch = j.str("branch"), salesperson = j.str("salesperson"),
            customer = j.str("customer"),
            items = j.optJSONArray("items").objects().map { it.str("description") to it.optInt("qty") },
            units = j.optInt("units"), picked = j.optInt("picked"), ageMinutes = j.optInt("age_minutes"),
            claimedBy = j.optJSONObject("claimed_by")?.str("name"), mine = j.optBoolean("mine"),
            emi = j.optBoolean("emi"), financier = j.str("financier").ifEmpty { null },
            invoice = j.str("invoice").ifEmpty { null }, toDeliver = j.optBoolean("to_deliver"),
        )
    }
}

data class Product(val id: Long, val name: String, val modelNo: String, val barcode: String, val sku: String,
                   val trackSerial: Boolean) {
    companion object {
        fun from(j: JSONObject?) = j?.let {
            Product(it.getLong("id"), it.str("name"), it.str("model_no"), it.str("barcode"), it.str("sku"),
                it.optBoolean("track_serial", true))
        }
    }
}

data class PickRow(val id: Long, val serial: String, val ok: Boolean, val result: String, val message: String) {
    companion object {
        fun from(j: JSONObject) = PickRow(j.getLong("id"), j.str("serial_no"), j.optBoolean("ok"), j.str("result"),
            j.str("message"))
    }
}

data class Line(
    val id: Long, val description: String, val qty: Int, val picked: Int, val needsScan: Boolean,
    val product: Product?, val allowed: List<String>, val truncated: Boolean, val picks: List<PickRow>,
    val racks: List<Pair<String, Int>> = emptyList(),
) {
    companion object {
        fun from(j: JSONObject) = Line(
            id = j.getLong("id"), description = j.str("description"), qty = j.optInt("qty"),
            picked = j.optInt("picked"), needsScan = j.optBoolean("needs_scan"),
            product = Product.from(j.optJSONObject("product")),
            allowed = j.optJSONArray("allowed_serials").strings(), truncated = j.optBoolean("truncated"),
            picks = j.optJSONArray("picks").objects().map { PickRow.from(it) },
            racks = j.optJSONArray("racks").objects().map { it.str("rack") to it.optInt("units") },
        )
    }
}

/** A unit that must leave with an invoiced order (the delivery check). */
data class DeliverUnit(val serial: String, val lineId: Long, val rack: String, val product: String, val checked: Boolean)

data class SwapInfo(val id: Long, val old: String, val new: String, val reason: String)

data class OrderDetail(val card: OrderCard, val lines: List<Line>, val notes: String, val idleMinutes: Int,
                       val deliver: List<DeliverUnit> = emptyList(), val pendingSwaps: List<SwapInfo> = emptyList()) {
    companion object {
        fun from(j: JSONObject) = OrderDetail(OrderCard.from(j), j.optJSONArray("lines").objects().map { Line.from(it) },
            j.str("notes"), j.optInt("idle_minutes"),
            deliver = j.optJSONArray("deliver").objects().map {
                DeliverUnit(it.str("serial_no"), it.optLong("line_id"), it.str("rack"), it.str("product"), it.optBoolean("checked"))
            },
            pendingSwaps = j.optJSONArray("pending_swaps").objects().map {
                SwapInfo(it.getLong("id"), it.str("old"), it.str("new"), it.str("reason"))
            })
    }
}

data class ReceiptUnit(val id: Long, val serial: String, val by: String)

data class ReceiptLine(val id: Long, val description: String, val qty: Int, val received: Int,
                       val product: Product?, val units: List<ReceiptUnit>)

data class Receipt(val id: Long, val orderNumber: String, val supplier: String, val units: Int, val received: Int,
                   val lines: List<ReceiptLine>) {
    companion object {
        fun from(j: JSONObject) = Receipt(
            id = j.getLong("id"), orderNumber = j.str("order_number"), supplier = j.str("supplier"),
            units = j.optInt("units"), received = j.optInt("received"),
            lines = j.optJSONArray("lines").objects().map { l ->
                ReceiptLine(l.getLong("id"), l.str("description"), l.optInt("qty"), l.optInt("received"),
                    l.optJSONObject("product")?.let { p ->
                        Product(p.getLong("id"), p.str("name"), p.str("model_no"), p.str("barcode"), "", true)
                    },
                    l.optJSONArray("units").objects().map { u -> ReceiptUnit(u.getLong("id"), u.str("serial_no"), u.str("by")) })
            },
        )
    }
}

data class CountInfo(val id: Long, val scanned: Int, val finished: Boolean, val startedBy: String) {
    companion object {
        fun from(j: JSONObject) = CountInfo(j.getLong("id"), j.optInt("scanned"), j.optBoolean("finished"),
            j.str("started_by"))
    }
}
