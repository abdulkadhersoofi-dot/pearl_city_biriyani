package com.arfa.scan

import android.content.Intent
import android.content.res.ColorStateList
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.view.LayoutInflater
import android.view.View
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.arfa.scan.databinding.ActivityBillBinding
import com.arfa.scan.databinding.ItemFieldBinding
import com.davemorrissey.labs.subscaleview.ImageSource
import com.davemorrissey.labs.subscaleview.SubsamplingScaleImageView
import com.google.android.material.chip.Chip
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException

/** One bill: its picture on top (tap any value and it zooms there), what
 * is wrong with it said plainly, the values to correct, and - for whoever
 * may approve it - Approve & post. A bill that couldn't be read offers
 * Retake, which replaces it. */
class BillActivity : AppCompatActivity() {

    private lateinit var b: ActivityBillBinding
    private var id = 0
    private var bill: JSONObject? = null
    private val edits = linkedMapOf<String, Pair<String, TextView>>()   // key -> (as read, its field)
    private var page = 0
    private var pages = 1
    private var pendingBox: JSONArray? = null
    private var kind = "purchase"
    private var paidBy = "cash"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityBillBinding.inflate(layoutInflater)
        setContentView(b.root)
        id = intent.getIntExtra("id", 0)
        b.back.setOnClickListener { finish() }
        b.more.setOnClickListener { menu(it) }
        b.image.setOnImageEventListener(object : SubsamplingScaleImageView.DefaultOnImageEventListener() {
            override fun onReady() {
                b.imageProgress.visibility = View.GONE
                pendingBox?.let { box -> pendingBox = null; b.image.pointAt(box) }
            }
        })
        b.pagePrev.setOnClickListener { showPage(page - 1, null) }
        b.pageNext.setOnClickListener { showPage(page + 1, null) }
        lifecycleScope.launch { load() }
    }

    private fun color(res: Int) = ContextCompat.getColor(this, res)

    private suspend fun load() {
        try {
            val body = withContext(Dispatchers.IO) { Api.get("/bills/$id") }
            bill = body
            bind(body)
            if (body.optString("state") == "reading") {
                delay(3000)
                load()
            }
        } catch (e: ApiError) {
            if (e.signedOut) { Api.session.signOut(); startActivity(Intent(this, LoginActivity::class.java)); finish() }
            else { Toast.makeText(this, e.message, Toast.LENGTH_LONG).show(); finish() }
        } catch (e: IOException) {
            Toast.makeText(this, R.string.offline, Toast.LENGTH_LONG).show()
        }
    }

    private fun bind(body: JSONObject) {
        val state = body.optString("state")
        val canApprove = body.optBoolean("can_approve")
        b.title.text = body.optString("supplier").ifEmpty { body.optString("filename") }
        b.subtitle.text = listOf(body.optString("invoice_number"), body.optString("category"),
            body.optString("from")).filter { it.isNotEmpty() }.joinToString(" · ")
        stateTag(state, body.optInt("issues"))
        pages = maxOf(1, body.optInt("pages", 1))
        b.pages.visibility = if (pages > 1) View.VISIBLE else View.GONE
        showPage(page, null)

        // What is wrong, plainly.
        b.issues.removeAllViews()
        val issues = body.optJSONArray("issues_list") ?: JSONArray()
        val retake = state == "retake"
        if (issues.length() > 0 || retake || body.optString("duplicate_of").isNotEmpty()) {
            b.issues.visibility = View.VISIBLE
            b.issues.setBackgroundResource(if (retake) R.drawable.card_bad else R.drawable.card_warn)
            if (retake) issueLine("Couldn't read this bill", body.optString("message"), true)
            body.optString("duplicate_of").takeIf { it.isNotEmpty() }?.let {
                issueLine("Looks already entered", it, false)
            }
            for (i in 0 until issues.length()) {
                val issue = issues.getJSONObject(i)
                issueLine(issue.optString("label"), issue.optString("detail"), issue.optBoolean("hard"))
            }
            if (retake) issueLine("", "Tip: tilt the bill away from the light and hold the phone straight above it.", false)
        } else {
            b.issues.visibility = View.GONE
        }

        // The values - the wrong ones marked; the owner (or an allowed branch) corrects them.
        b.fields.removeAllViews()
        edits.clear()
        val fields = body.optJSONArray("fields") ?: JSONArray()
        for (i in 0 until fields.length()) field(fields.getJSONObject(i), canApprove && state != "posted")

        // The lines.
        val lines = body.optJSONArray("lines") ?: JSONArray()
        b.linesTitle.text = if (lines.length() > 0) "${lines.length()} item${if (lines.length() > 1) "s" else ""}" +
            (body.optJSONObject("supplier_match")?.let { " · supplier in your books: ${it.optString("name")}" } ?: " · a new supplier")
            else ""
        b.linesTitle.visibility = if (lines.length() > 0) View.VISIBLE else View.GONE
        b.lines.removeAllViews()
        for (i in 0 until lines.length()) line(lines.getJSONObject(i), i == lines.length() - 1)
        b.lines.visibility = if (lines.length() > 0) View.VISIBLE else View.GONE

        val totals = body.optJSONObject("totals")
        if (totals != null && lines.length() > 0) {
            val diff = totals.optString("difference").toDoubleOrNull() ?: 0.0
            val tds = totals.optString("tds").toDoubleOrNull() ?: 0.0
            b.totals.text = buildString {
                append("ARFA's total ₹").append(Format.money(totals.optString("total").toDoubleOrNull() ?: 0.0))
                append(" · printed ₹").append(Format.money(totals.optString("printed").toDoubleOrNull() ?: 0.0))
                if (tds > 0) append(" · TDS ₹").append(Format.money(tds))
                if (diff > 1) append("\nThey differ by ₹").append(Format.money(diff)).append(" - check the marked values.")
            }
            b.totals.setTextColor(color(if (diff > 1) R.color.tag_warn else R.color.ink))
            b.totals.visibility = View.VISIBLE
        } else {
            b.totals.visibility = View.GONE
        }

        // Post it as, and how it was paid (an expense).
        val kinds = body.optJSONArray("kinds") ?: JSONArray()
        kind = body.optString("kind").ifEmpty { "purchase" }
        val showKinds = canApprove && state != "posted" && state != "retake" && kinds.length() > 1
        b.kindTitle.visibility = if (showKinds) View.VISIBLE else View.GONE
        b.kinds.visibility = if (showKinds) View.VISIBLE else View.GONE
        b.kinds.removeAllViews()
        if (showKinds) {
            for (i in 0 until kinds.length()) {
                val k = kinds.getString(i)
                b.kinds.addView(Chip(this).apply {
                    text = when (k) { "purchase" -> "Purchase bill"; "expense" -> "Expense"; else -> "Keep on file" }
                    isCheckable = true
                    isChecked = k == kind
                    setOnClickListener { kind = k; paidByChips() }
                })
            }
        }
        paidByChips()

        // What can be done with it.
        b.note.visibility = View.GONE
        b.middle.visibility = View.GONE
        b.primary.visibility = View.VISIBLE
        b.secondary.visibility = View.VISIBLE
        when {
            state == "retake" -> {
                b.secondary.text = getString(R.string.discard)
                b.secondary.setOnClickListener { discard() }
                b.middle.visibility = View.VISIBLE
                b.middle.text = getString(R.string.keep_on_file)
                b.middle.setOnClickListener { act("/bills/$id/keep", "Kept on file.") }
                b.primary.text = getString(R.string.retake)
                b.primary.setOnClickListener {
                    startActivity(Intent(this, MainActivity::class.java).putExtra("retake", id)
                        .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
                    finish()
                }
            }
            state == "reading" -> {
                note("ARFA is reading this bill - a few seconds.")
                b.primary.visibility = View.GONE
                b.secondary.text = getString(R.string.later)
                b.secondary.setOnClickListener { finish() }
            }
            body.optString("status") == "to_check" && canApprove -> {
                b.secondary.text = getString(R.string.later)
                b.secondary.setOnClickListener { finish() }
                b.primary.text = getString(R.string.approve)
                b.primary.setOnClickListener { approve(false) }
            }
            body.optString("status") == "to_check" -> {
                note(getString(R.string.waiting_owner))
                b.primary.visibility = View.GONE
                b.secondary.text = getString(R.string.later)
                b.secondary.setOnClickListener { finish() }
            }
            else -> {
                note(when (state) { "posted" -> "Posted."; "filed" -> "Kept on file."; else -> "" })
                b.primary.visibility = View.GONE
                b.secondary.text = getString(R.string.later)
                b.secondary.setOnClickListener { finish() }
            }
        }
    }

    private fun note(text: String) {
        b.note.text = text
        b.note.visibility = if (text.isEmpty()) View.GONE else View.VISIBLE
    }

    private fun stateTag(state: String, issues: Int) {
        val (text, fg, bg) = when (state) {
            "passed" -> Triple("All checks passed", R.color.tag_ok, R.color.tag_ok_bg)
            "look" -> Triple(if (issues > 0) "$issues to look at" else "Have a look", R.color.tag_warn, R.color.tag_warn_bg)
            "duplicate" -> Triple("Already entered?", R.color.tag_warn, R.color.tag_warn_bg)
            "retake" -> Triple("Retake", R.color.tag_bad, R.color.tag_bad_bg)
            "posted" -> Triple("Posted", R.color.tag_ok, R.color.tag_ok_bg)
            "reading" -> Triple("Reading…", R.color.muted, R.color.tag_muted_bg)
            else -> Triple(state, R.color.muted, R.color.tag_muted_bg)
        }
        b.state.text = text
        b.state.setTextColor(color(fg))
        b.state.backgroundTintList = ColorStateList.valueOf(color(bg))
    }

    private fun issueLine(title: String, detail: String, hard: Boolean) {
        val view = TextView(this).apply {
            text = if (title.isEmpty()) detail else if (detail.isEmpty()) title else "$title - $detail"
            setTextColor(color(if (hard) R.color.tag_bad else R.color.tag_warn))
            textSize = 14f
            setPadding(0, 4, 0, 4)
        }
        b.issues.addView(view)
    }

    private fun field(f: JSONObject, editable: Boolean) {
        val row = ItemFieldBinding.inflate(LayoutInflater.from(this), b.fields, false)
        val key = f.optString("key")
        row.label.text = f.optString("label")
        row.qr.visibility = if (f.optBoolean("from_qr")) View.VISIBLE else View.GONE
        val value = f.optString("value")
        row.value.setText(if (key in MONEY && value.toDoubleOrNull() != null) Format.money(value.toDouble()) else value)
        val canEdit = editable && f.optBoolean("editable")
        row.value.isEnabled = canEdit
        row.value.isFocusable = canEdit
        row.value.inputType = when {
            key in MONEY -> InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
            key == "supplier_gstin" -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS
            else -> InputType.TYPE_CLASS_TEXT
        }
        if (f.optBoolean("flagged")) {
            row.row.setBackgroundColor(color(R.color.flag_bg))
            row.label.setTextColor(color(R.color.tag_warn))
            row.value.setTextColor(color(R.color.tag_warn))
        }
        val box = f.optJSONArray("box")
        val showIt = { if (box != null) point(box) }
        row.row.setOnClickListener { showIt(); if (canEdit) row.value.requestFocus() }
        row.value.setOnFocusChangeListener { _, has -> if (has) showIt() }
        if (canEdit) edits[key] = (row.value.text.toString()) to row.value
        b.fields.addView(row.root)
    }

    private fun line(l: JSONObject, last: Boolean) {
        val view = TextView(this).apply {
            val disc = l.optString("discount").toDoubleOrNull() ?: 0.0
            val discText = if (disc > 0) (if (l.optString("disc_type") == "amt") " - ₹${Format.money(disc)}" else " - $disc%") else ""
            text = "${l.optString("description")}\n${l.optString("qty")} ${l.optString("unit")} × ₹${l.optString("rate")}$discText" +
                " · GST ${l.optString("gst_rate")}% · ₹${Format.money(l.optString("amount").toDoubleOrNull() ?: 0.0)}" +
                (l.optString("serials").takeIf { it.isNotEmpty() }?.let { "\nSerial nos.: " + it.replace("\n", ", ") } ?: "")
            setTextColor(color(R.color.ink))
            textSize = 14f
            setPadding(36, 22, 36, 22)
            setBackgroundResource(android.R.drawable.list_selector_background)
            val box = l.optJSONArray("box")
            setOnClickListener { if (box != null) point(box) }
        }
        b.lines.addView(view)
        if (!last) b.lines.addView(View(this).apply {
            setBackgroundColor(0xFFECEBE6.toInt())
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1)
        })
    }

    private fun paidByChips() {
        val show = kind == "expense" && b.kinds.visibility == View.VISIBLE
        b.paidBy.visibility = if (show) View.VISIBLE else View.GONE
        if (!show || b.paidBy.childCount > 0) return
        for ((value, label) in listOf("cash" to "Paid in cash", "upi" to "UPI", "bank" to "Bank", "card" to "Card",
                                      "unpaid" to "Not paid yet")) {
            b.paidBy.addView(Chip(this).apply {
                text = label
                isCheckable = true
                isChecked = value == paidBy
                setOnClickListener { paidBy = value }
            })
        }
    }

    // --- the picture --------------------------------------------------------------------------------

    private fun point(box: JSONArray) {
        val onPage = box.optInt(0)
        if (onPage != page) showPage(onPage, box) else if (!b.image.pointAt(box)) pendingBox = box
    }

    private fun showPage(n: Int, box: JSONArray?) {
        val body = bill ?: return
        val target = n.coerceIn(0, pages - 1)
        b.pageLabel.text = "${target + 1} / $pages"
        if (target == page && b.image.isReady) { box?.let { b.image.pointAt(it) }; return }
        page = target
        pendingBox = box
        val url = body.optString("page_url").replace("{n}", target.toString())
        b.imageProgress.visibility = View.VISIBLE
        lifecycleScope.launch {
            try {
                val file = File(cacheDir, "bill-$id-$target-${url.substringAfter("v=", "0")}.jpg")
                if (!file.exists()) withContext(Dispatchers.IO) { file.writeBytes(Api.bytes(url)) }
                b.image.setImage(ImageSource.uri(Uri.fromFile(file)))
            } catch (e: Exception) {
                b.imageProgress.visibility = View.GONE
            }
        }
    }

    // --- acting on it -----------------------------------------------------------------------------------

    private fun approve(anyway: Boolean) {
        val changes = JSONObject().put("kind", kind)
        if (kind == "expense") changes.put("payment_mode", paidBy)
        for ((key, pair) in edits) {
            val now = pair.second.text.toString().trim()
            val was = pair.first.trim()
            if (now == was) continue
            val value = if (key in MONEY) now.replace(",", "") else now
            changes.put(API_KEY[key] ?: key, value)
        }
        if (anyway) changes.put("accept_difference", true)
        b.primary.isEnabled = false
        lifecycleScope.launch {
            try {
                withContext(Dispatchers.IO) { Api.post("/bills/$id/approve", changes) }
                Toast.makeText(this@BillActivity, if (kind == "file") "Kept on file." else "Posted.", Toast.LENGTH_SHORT).show()
                finish()
            } catch (e: ApiError) {
                if (e.code == "difference") {
                    AlertDialog.Builder(this@BillActivity)
                        .setTitle(R.string.post_anyway_title)
                        .setMessage(e.message)
                        .setPositiveButton(R.string.post_anyway) { _, _ -> approve(true) }
                        .setNegativeButton(R.string.cancel, null)
                        .show()
                } else {
                    AlertDialog.Builder(this@BillActivity).setMessage(e.message).setPositiveButton(android.R.string.ok, null).show()
                }
            } catch (e: IOException) {
                Toast.makeText(this@BillActivity, R.string.offline, Toast.LENGTH_LONG).show()
            } finally {
                b.primary.isEnabled = true
            }
        }
    }

    private fun act(path: String, done: String, body: JSONObject = JSONObject()) {
        lifecycleScope.launch {
            try {
                withContext(Dispatchers.IO) { Api.post(path, body) }
                Toast.makeText(this@BillActivity, done, Toast.LENGTH_SHORT).show()
                if (path.endsWith("/turn") || path.endsWith("/reread")) {
                    if (path.endsWith("/reread")) finish() else { page = -1; load() }
                } else finish()
            } catch (e: ApiError) {
                Toast.makeText(this@BillActivity, e.message, Toast.LENGTH_LONG).show()
            } catch (e: IOException) {
                Toast.makeText(this@BillActivity, R.string.offline, Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun discard() {
        AlertDialog.Builder(this)
            .setMessage(R.string.discard_confirm)
            .setPositiveButton(R.string.discard) { _, _ -> act("/bills/$id/discard", "Discarded.") }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun menu(anchor: View) {
        val state = bill?.optString("state") ?: return
        val open = state != "posted" && state != "filed"
        PopupMenu(this, anchor).apply {
            if (open) {
                menu.add(0, 1, 0, R.string.turn_left)
                menu.add(0, 2, 1, R.string.turn_right)
                menu.add(0, 3, 2, R.string.read_again)
                menu.add(0, 4, 3, R.string.discard)
            }
            menu.add(0, 5, 4, "Whole page")
            setOnMenuItemClickListener {
                when (it.itemId) {
                    1 -> act("/bills/$id/turn", "Turned.", JSONObject().put("deg", 270))
                    2 -> act("/bills/$id/turn", "Turned.", JSONObject().put("deg", 90))
                    3 -> act("/bills/$id/reread", "It will be read again.")
                    4 -> discard()
                    5 -> b.image.overview()
                }
                true
            }
        }.show()
    }

    companion object {
        private val MONEY = setOf("taxable_value", "cgst", "sgst", "igst", "tds", "invoice_value")
        /** The field read -> what the approve call calls it. */
        private val API_KEY = mapOf("invoice_value" to "bill_total", "tds" to "tds_amount")
    }
}
