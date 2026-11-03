package com.arfa.scan

import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

object Format {
    private val time = DateTimeFormatter.ofPattern("h:mm a", Locale.ENGLISH)
    private val day = DateTimeFormatter.ofPattern("d MMM yyyy", Locale.ENGLISH)

    /** Rupees the Indian way: 2,25,556.23. */
    fun money(value: Double): String = indian(value)

    private fun indian(value: Double): String {
        val fixed = String.format(Locale.ENGLISH, "%.2f", Math.abs(value))
        val (whole, paise) = fixed.split(".").let { it[0] to it[1] }
        val grouped = if (whole.length <= 3) whole else {
            val head = whole.dropLast(3)
            head.reversed().chunked(2).joinToString(",").reversed() + "," + whole.takeLast(3)
        }
        return (if (value < 0) "-" else "") + grouped + "." + paise
    }

    private fun local(iso: String): java.time.ZonedDateTime? =
        runCatching { OffsetDateTime.parse(iso).atZoneSameInstant(ZoneId.systemDefault()) }.getOrNull()

    fun timeOf(iso: String): String = local(iso)?.format(time) ?: ""

    /** "Today", "Yesterday" or the date - the chat's day separators. */
    fun dayOf(iso: String, today: String, yesterday: String): String {
        val at = local(iso)?.toLocalDate() ?: return ""
        val now = java.time.LocalDate.now()
        return when (at) {
            now -> today
            now.minusDays(1) -> yesterday
            else -> at.format(day)
        }
    }

    /** A bill date from the server (YYYY-MM-DD) as people write it. */
    fun billDate(iso: String): String = runCatching {
        java.time.LocalDate.parse(iso).format(DateTimeFormatter.ofPattern("d MMM", Locale.ENGLISH))
    }.getOrDefault(iso)
}
