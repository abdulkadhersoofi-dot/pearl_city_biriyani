package com.arfa.warehouse.sync

/**
 * The offline queue's rules, kept apart from Room so they're unit-tested.
 *
 * A scan is checked on the phone first and shown at once, queued with its
 * client id; the server's answer is final. A pick the server refuses
 * (someone else got that unit) goes back to "needs scan" on its line.
 * Confirm stays locked until every unit on every line is server-confirmed.
 */
object QueueRules {
    const val QUEUED = "queued"
    const val CONFIRMED = "confirmed"
    const val REJECTED = "rejected"

    data class Pick(val clientId: String, val lineId: Long, val serial: String?, val state: String,
                    val message: String? = null)

    /** The state after the server answered: ok = accepted (ok / added). */
    fun afterServer(pick: Pick, ok: Boolean, message: String?): Pick =
        pick.copy(state = if (ok) CONFIRMED else REJECTED, message = message)

    data class LineView(val qty: Int, val confirmed: Int, val waiting: Int) {
        val shown: Int get() = minOf(qty, confirmed + waiting)
        val needsScan: Boolean get() = shown < qty
    }

    /** One line as the picker sees it: the server's own count plus what is
     * still on its way (rejected picks don't count - the line needs a scan). */
    fun line(qty: Int, serverPicked: Int, queue: List<Pick>, lineId: Long): LineView {
        val waiting = queue.count { it.lineId == lineId && it.state == QUEUED }
        return LineView(qty, serverPicked, waiting)
    }

    fun waiting(queue: List<Pick>): Int = queue.count { it.state == QUEUED }

    /** Confirm: every unit server-confirmed and nothing still syncing. */
    fun canConfirm(lines: List<LineView>, queue: List<Pick>): Boolean =
        lines.all { it.confirmed >= it.qty } && waiting(queue) == 0

    /** A serial already queued or confirmed on this order isn't sent twice. */
    fun alreadyScanned(queue: List<Pick>, serverSerials: Collection<String>, serial: String): Boolean =
        serial in serverSerials || queue.any { it.serial == serial && it.state != REJECTED }
}
