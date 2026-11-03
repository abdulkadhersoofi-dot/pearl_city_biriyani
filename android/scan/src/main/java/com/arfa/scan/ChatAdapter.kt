package com.arfa.scan

import android.content.res.ColorStateList
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import coil.load
import com.arfa.scan.databinding.ItemBillBinding
import com.arfa.scan.databinding.ItemDayBinding
import java.io.File

/** A row of the chat: a day separator, a bill on the server, or a bill
 * still on the phone. */
sealed class Row(val key: String) {
    class Day(val label: String) : Row("day:$label")
    class Server(val bill: Bill) : Row("bill:${bill.id}")
    class Pending(val item: Outgoing) : Row("local:${item.localId}")
}

class ChatAdapter(
    private val onOpen: (Bill) -> Unit,
    private val onRetake: (Bill) -> Unit,
    private val onPending: (Outgoing) -> Unit,
    private val owner: () -> Boolean,
) : ListAdapter<Row, RecyclerView.ViewHolder>(DIFF) {

    override fun getItemViewType(position: Int) = if (getItem(position) is Row.Day) 0 else 1

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
        val inflater = LayoutInflater.from(parent.context)
        return if (viewType == 0) DayHolder(ItemDayBinding.inflate(inflater, parent, false))
        else BillHolder(ItemBillBinding.inflate(inflater, parent, false))
    }

    override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
        when (val row = getItem(position)) {
            is Row.Day -> (holder as DayHolder).b.day.text = row.label
            is Row.Server -> (holder as BillHolder).bind(row.bill)
            is Row.Pending -> (holder as BillHolder).bind(row.item)
        }
    }

    class DayHolder(val b: ItemDayBinding) : RecyclerView.ViewHolder(b.root)

    inner class BillHolder(private val b: ItemBillBinding) : RecyclerView.ViewHolder(b.root) {
        private val ctx = b.root.context
        private fun color(id: Int) = ContextCompat.getColor(ctx, id)

        private fun tag(text: String, fg: Int, bg: Int) {
            b.tag.visibility = if (text.isEmpty()) View.GONE else View.VISIBLE
            b.tag.text = text
            b.tag.setTextColor(color(fg))
            b.tag.backgroundTintList = ColorStateList.valueOf(color(bg))
        }

        private fun ticks(icon: Int, tint: Int) {
            b.ticks.setImageResource(icon)
            b.ticks.imageTintList = ColorStateList.valueOf(color(tint))
        }

        fun bind(bill: Bill) {
            b.card.setCardBackgroundColor(color(R.color.bubble))
            b.thumb.load(Api.url(bill.thumb)) { placeholder(R.drawable.thumb_bg); error(R.drawable.thumb_bg) }
            val number = listOf(bill.invoiceNumber, Format.billDate(bill.invoiceDate)).filter { it.isNotEmpty() }
            b.supplier.setTextColor(color(R.color.ink))
            b.total.visibility = View.VISIBLE
            b.total.text = bill.total?.let { "₹" + Format.money(it) } ?: ""
            b.time.text = Format.timeOf(bill.uploadedAt)
            b.from.text = if (owner() && bill.from.isNotEmpty()) bill.from else ""
            b.action.visibility = View.GONE
            when (bill.state) {
                "reading" -> {
                    b.supplier.text = "Reading…"
                    b.meta.text = bill.filename
                    b.total.visibility = View.GONE
                    tag("", R.color.muted, R.color.tag_muted_bg)
                    ticks(R.drawable.ic_tick, R.color.tick_sent)
                }
                "retake" -> {
                    b.supplier.text = "Couldn't read this bill"
                    b.supplier.setTextColor(color(R.color.tag_bad))
                    b.meta.text = bill.message.ifEmpty { bill.issue }
                    b.total.visibility = View.GONE
                    tag("", R.color.muted, R.color.tag_muted_bg)
                    b.action.visibility = View.VISIBLE
                    b.action.text = ctx.getString(R.string.retake)
                    b.action.setTextColor(color(R.color.tag_bad))
                    b.action.setOnClickListener { onRetake(bill) }
                    ticks(R.drawable.ic_ticks, R.color.tick_read)
                }
                else -> {
                    b.supplier.text = bill.supplier.ifEmpty { bill.filename }
                    b.meta.text = number.joinToString(" · ")
                    when (bill.state) {
                        "passed" -> tag("All checks passed", R.color.tag_ok, R.color.tag_ok_bg)
                        "look" -> tag(if (bill.issues > 0) "${bill.issues} to look at" + (bill.issue.takeIf { it.isNotEmpty() }
                            ?.let { " · ${it.lowercase()}" } ?: "") else "Have a look", R.color.tag_warn, R.color.tag_warn_bg)
                        "duplicate" -> tag("Already entered?", R.color.tag_warn, R.color.tag_warn_bg)
                        "posted" -> tag("Posted", R.color.tag_ok, R.color.tag_ok_bg)
                        "filed" -> tag("On file", R.color.muted, R.color.tag_muted_bg)
                        else -> tag(bill.state, R.color.muted, R.color.tag_muted_bg)
                    }
                    ticks(R.drawable.ic_ticks, if (bill.state == "posted") R.color.arfa_primary else R.color.tick_read)
                }
            }
            b.card.setOnClickListener { onOpen(bill) }
        }

        fun bind(item: Outgoing) {
            b.card.setCardBackgroundColor(color(R.color.bubble_pending))
            val preview = item.preview?.let { File(it) }
            if (preview != null && preview.exists()) b.thumb.load(preview) else b.thumb.setImageResource(R.drawable.thumb_bg)
            b.supplier.setTextColor(color(R.color.ink))
            b.total.visibility = View.GONE
            b.from.text = ""
            b.time.text = android.text.format.DateFormat.format("h:mm a", item.created).toString()
            b.action.visibility = View.GONE
            when (item.state) {
                "failed" -> {
                    b.supplier.text = "Not sent"
                    b.supplier.setTextColor(color(R.color.tag_bad))
                    b.meta.text = item.error ?: "Tap to try again."
                    tag("Tap to retry", R.color.tag_bad, R.color.tag_bad_bg)
                    ticks(R.drawable.ic_warn, R.color.tag_bad)
                }
                "reading" -> {
                    b.supplier.text = "Reading…"
                    b.meta.text = item.summary.ifEmpty { "Sent - ARFA is reading it" }
                    tag("", R.color.muted, R.color.tag_muted_bg)
                    ticks(R.drawable.ic_tick, R.color.tick_sent)
                }
                else -> {
                    b.supplier.text = if (item.replaces != null) "Retaken - sending…" else "Sending…"
                    b.meta.text = item.summary.ifEmpty { if (item.ocr == null) "Reading on the phone…" else "Waiting for the internet" }
                    tag("", R.color.muted, R.color.tag_muted_bg)
                    ticks(R.drawable.ic_clock, R.color.tick_sent)
                }
            }
            b.card.setOnClickListener { onPending(item) }
        }
    }

    companion object {
        val DIFF = object : DiffUtil.ItemCallback<Row>() {
            override fun areItemsTheSame(a: Row, b: Row) = a.key == b.key
            override fun areContentsTheSame(a: Row, b: Row) = when {
                a is Row.Server && b is Row.Server -> a.bill == b.bill
                a is Row.Pending && b is Row.Pending -> a.item == b.item
                a is Row.Day && b is Row.Day -> a.label == b.label
                else -> false
            }
        }
    }
}
