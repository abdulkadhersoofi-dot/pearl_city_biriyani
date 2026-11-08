package com.arfa.warehouse

import com.arfa.warehouse.scan.Matcher
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MatcherTest {
    private val index = Matcher.Index(listOf("ACA10O25", "LG-778-12", "IMEI356789012345678"))
    private val codes = Matcher.productCodes("AR18TY3QBWK", "8901234567890", null)

    @Test fun normalising_is_what_the_server_compares() {
        assertEquals("AB12CD", Matcher.normalise(" ab 12\tcd "))
    }

    @Test fun look_alike_characters_fold_for_matching_only() {
        assertEquals("ACA10025", Matcher.compact("aca1o o25"))
        assertEquals("1G77812", Matcher.compact("LG-778-12"))
    }

    @Test fun an_exact_barcode_matches_and_gives_back_the_stored_serial() {
        val o = Matcher.fromBarcodes(listOf("8901234567890", "ACA10O25"), index, codes)
        assertEquals(Matcher.Outcome.Match("ACA10O25"), o)
    }

    @Test fun text_read_with_misread_letters_and_a_label_still_matches_exactly() {
        val text = "LG Electronics\nModel: AR18TY3QBWK\nS/N: ACA1 0025\nMade in India"
        assertEquals(Matcher.Outcome.Match("ACA10O25"), Matcher.fromText(text, index, codes))
        assertEquals(Matcher.Outcome.Match("LG-778-12"), Matcher.fromText("SR.NO LG 778 12", index, codes))
    }

    @Test fun close_is_not_a_match() {
        assertEquals(Matcher.Outcome.None, Matcher.fromText("ACA10026", index, emptySet()))
        assertEquals(Matcher.Outcome.None, Matcher.fromBarcodes(listOf("ACA1002"), index, emptySet()))
    }

    @Test fun the_right_model_with_a_serial_not_in_stock_is_told_apart() {
        val o = Matcher.fromBarcodes(listOf("AR18TY3QBWK", "8901234567890", "NEWSERIAL99"), index, codes)
        assertEquals(Matcher.Outcome.RightModel("NEWSERIAL99"), o)
        assertTrue(Matcher.fromText("MODEL AR18TY3QBWK", index, codes) is Matcher.Outcome.RightModel)
    }

    @Test fun serials_that_fold_to_the_same_form_are_never_guessed_between() {
        val clash = Matcher.Index(listOf("AB0X12", "ABOX12"))
        assertNull(clash.find("AB0X12"))
    }
}
