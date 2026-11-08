package com.arfa.warehouse

import com.arfa.warehouse.sync.QueueRules
import com.arfa.warehouse.sync.QueueRules.Pick
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class QueueRulesTest {
    @Test fun a_queued_scan_shows_at_once_but_confirm_waits_for_the_server() {
        val queue = listOf(Pick("p1", 7, "S1", QueueRules.QUEUED))
        val line = QueueRules.line(qty = 2, serverPicked = 1, queue = queue, lineId = 7)
        assertEquals(2, line.shown)
        assertFalse(line.needsScan)
        assertFalse(QueueRules.canConfirm(listOf(line), queue))
    }

    @Test fun a_scan_the_server_refuses_rolls_the_line_back_to_needs_scan() {
        val sent = Pick("p1", 7, "S1", QueueRules.QUEUED)
        val refused = QueueRules.afterServer(sent, ok = false, message = "S1 is held for SO/0040.")
        assertEquals(QueueRules.REJECTED, refused.state)
        val line = QueueRules.line(2, 1, listOf(refused), 7)
        assertTrue(line.needsScan)
        assertEquals(1, line.shown)
        assertFalse(QueueRules.alreadyScanned(listOf(refused), emptyList(), "S1"))   // may be scanned again
    }

    @Test fun every_unit_confirmed_unlocks_confirm() {
        val done = QueueRules.afterServer(Pick("p1", 7, "S2", QueueRules.QUEUED), ok = true, message = "S2 picked.")
        val line = QueueRules.line(2, 2, listOf(done), 7)
        assertTrue(QueueRules.canConfirm(listOf(line), listOf(done)))
    }

    @Test fun a_serial_is_not_queued_twice() {
        val queue = listOf(Pick("p1", 7, "S1", QueueRules.QUEUED))
        assertTrue(QueueRules.alreadyScanned(queue, emptyList(), "S1"))
        assertTrue(QueueRules.alreadyScanned(emptyList(), listOf("S9"), "S9"))
    }
}
