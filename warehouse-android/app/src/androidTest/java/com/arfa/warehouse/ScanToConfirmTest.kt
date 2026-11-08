package com.arfa.warehouse

import androidx.room.Room
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.arfa.warehouse.data.Api
import com.arfa.warehouse.data.AppDb
import com.arfa.warehouse.data.Pending
import com.arfa.warehouse.data.Session
import com.arfa.warehouse.sync.QueueRules
import com.arfa.warehouse.sync.Sync
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/** Scan -> queue -> sync -> confirm, against a fake ARFA server: a pick the
 * server accepts is confirmed, one it refuses rolls the line back, and
 * confirm goes through once every unit is server-confirmed. */
@RunWith(AndroidJUnit4::class)
class ScanToConfirmTest {
    private lateinit var server: MockWebServer
    private lateinit var db: AppDb
    private lateinit var api: Api

    @Before fun setUp() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        server = MockWebServer().apply { start() }
        db = Room.inMemoryDatabaseBuilder(context, AppDb::class.java).allowMainThreadQueries().build()
        val session = Session(context).apply { accessToken = "test-token" }
        api = Api(session, server.url("/").toString())
    }

    @After fun tearDown() {
        db.close()
        server.shutdown()
    }

    private fun pick(id: Long, serial: String, ok: Boolean, result: String) = MockResponse()
        .setResponseCode(if (ok) 201 else 200)
        .setBody("""{"pick": {"id": $id, "serial_no": "$serial", "ok": $ok, "result": "$result",
            "message": "${if (ok) "$serial picked." else "$serial is held for SO/0040."}"}}""")

    @Test fun scan_sync_and_confirm() = runBlocking {
        val outbox = db.outbox()
        outbox.add(Pending("p-1", "pick", 42, 7, "ACA1", "barcode"))
        outbox.add(Pending("p-2", "pick", 42, 7, "ACA2", "barcode"))
        server.enqueue(pick(1, "ACA1", true, "ok"))
        server.enqueue(pick(2, "ACA2", false, "duplicate"))
        val refused = mutableListOf<String>()

        assertTrue(Sync.run(api, outbox) { serial, _ -> refused += serial })

        val first = server.takeRequest()
        assertEquals("/api/wh/v1/orders/42/lines/7/picks", first.path)
        assertEquals("Bearer test-token", first.getHeader("Authorization"))
        assertTrue(first.body.readUtf8().contains("\"client_pick_id\":\"p-1\""))
        assertEquals(listOf("ACA2"), refused)
        assertTrue(outbox.queued().isEmpty())

        // The line: 1 of 2 confirmed, the refused one needs scanning again - confirm stays locked.
        val rows = listOf(QueueRules.Pick("p-1", 7, "ACA1", QueueRules.CONFIRMED),
                          QueueRules.Pick("p-2", 7, "ACA2", QueueRules.REJECTED))
        val line = QueueRules.line(qty = 2, serverPicked = 1, queue = rows, lineId = 7)
        assertTrue(line.needsScan)
        assertFalse(QueueRules.canConfirm(listOf(line), rows))

        // A second unit scanned and accepted, then confirm.
        outbox.add(Pending("p-3", "pick", 42, 7, "ACA3", "barcode"))
        server.enqueue(pick(3, "ACA3", true, "ok"))
        assertTrue(Sync.run(api, outbox) { _, _ -> })
        server.takeRequest(); server.takeRequest()
        val done = QueueRules.line(2, 2, emptyList(), 7)
        assertTrue(QueueRules.canConfirm(listOf(done), emptyList()))
        server.enqueue(MockResponse().setBody("""{"message": "Sent to Warehouse A computer for invoicing."}"""))
        val reply = api.post("/orders/42/confirm")
        assertTrue(reply.ok)
        assertEquals("/api/wh/v1/orders/42/confirm", server.takeRequest().path)
    }
}
