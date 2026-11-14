package com.arfa.warehouse.scan

/**
 * Identifying the unit is matching, not open-ended reading: the phone knows
 * the serials in stock at this warehouse for this line (allowed_serials).
 *
 *  - A barcode value equal to an allowed serial is a match.
 *  - Text read off the label: each token is normalised (upper case, spaces,
 *    dashes and separators out, O/0 I/1 L/1 S/5 folded) and must equal an
 *    allowed serial normalised the same way - exactly, never "close".
 *  - A value that is the product's own model no. / barcode means the right
 *    box with a serial that isn't in stock here.
 *
 * The server checks every pick again; this only makes the answer instant.
 */
object Matcher {
    private val SPACES = Regex("\\s+")
    private val SEPARATORS = Regex("[\\s\\-_/.:]")
    private val LABEL_WORDS = Regex(
        "^(S/?N|SR\\.?\\s*NO|SERIAL(\\s*NO\\.?|\\s*NUMBER)?|IMEI\\s*\\d?|SL\\.?\\s*NO|MODEL(\\s*NO\\.?)?|M/N)\\s*[:#.-]?\\s*",
        RegexOption.IGNORE_CASE,
    )

    /** What the server compares: spaces out, upper case. */
    fun normalise(text: String): String = text.replace(SPACES, "").uppercase()

    /** For matching only: separators out, look-alike characters folded. */
    fun compact(text: String): String = buildString {
        for (c in text.uppercase().replace(SEPARATORS, "")) {
            append(
                when (c) {
                    'O' -> '0'
                    'I', 'L' -> '1'
                    'S' -> '5'
                    else -> c
                }
            )
        }
    }

    /** The allowed serials, looked up by their compact form. Two serials that
     * fold to the same compact form are left out (they could be confused). */
    class Index(allowed: Collection<String>) {
        private val byCompact: Map<String, String>
        val size: Int

        init {
            val seen = HashMap<String, String>()
            val clashes = HashSet<String>()
            for (serial in allowed) {
                val key = compact(serial)
                if (key.isEmpty()) continue
                val before = seen.put(key, serial)
                if (before != null && before != serial) clashes.add(key)
            }
            clashes.forEach { seen.remove(it) }
            byCompact = seen
            size = allowed.size
        }

        fun find(value: String): String? = byCompact[compact(value)]
    }

    /** Candidate tokens from a block of label text: each line, its words,
     * neighbouring words joined (a serial printed "ABC 123 456"), and what
     * follows a "S/N:" or "Serial No." label. */
    fun tokens(text: String): List<String> {
        val out = LinkedHashSet<String>()
        for (raw in text.lines()) {
            val line = raw.trim()
            if (line.isEmpty()) continue
            out.add(line)
            val unlabelled = line.replace(LABEL_WORDS, "").trim()
            if (unlabelled.isNotEmpty()) out.add(unlabelled)
            val words = unlabelled.split(SPACES).filter { it.isNotEmpty() }
            for (i in words.indices) {
                out.add(words[i])
                if (i + 1 < words.size) out.add(words[i] + words[i + 1])
                if (i + 2 < words.size) out.add(words[i] + words[i + 1] + words[i + 2])
            }
            line.split(':', '#').drop(1).map { it.trim() }.filter { it.isNotEmpty() }.forEach { out.add(it) }
        }
        return out.filter { compact(it).length >= 4 }
    }

    sealed class Outcome {
        data class Match(val serial: String) : Outcome()
        /** The right model on the box, but its serial isn't in stock here. */
        data class RightModel(val candidate: String?) : Outcome()
        object None : Outcome()
    }

    fun productCodes(vararg codes: String?): Set<String> =
        codes.filterNotNull().map { compact(it) }.filter { it.isNotEmpty() }.toSet()

    /** Barcodes read in one frame. */
    fun fromBarcodes(values: List<String>, index: Index, productCodes: Set<String>): Outcome {
        for (value in values) {
            index.find(value)?.let { return Outcome.Match(it) }
        }
        val model = values.any { compact(it) in productCodes }
        if (model) {
            // The other code on the label is most likely the serial.
            val candidate = values.firstOrNull { v ->
                val c = compact(v)
                c !in productCodes && c.length in 6..30 && !(c.length == 13 && c.all { it.isDigit() })
            }
            return Outcome.RightModel(candidate?.let { normalise(it) })
        }
        return Outcome.None
    }

    /** Text read in one frame - exact matches only. */
    fun fromText(text: String, index: Index, productCodes: Set<String>): Outcome {
        val tokens = tokens(text)
        for (token in tokens) {
            index.find(token)?.let { return Outcome.Match(it) }
        }
        if (tokens.any { compact(it) in productCodes }) return Outcome.RightModel(null)
        return Outcome.None
    }
}
