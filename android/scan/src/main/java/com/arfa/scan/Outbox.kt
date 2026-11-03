package com.arfa.scan

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/** A bill taken on this phone that hasn't reached the server yet (or is
 * being read): kept in the app's own files, so nothing is lost if the
 * network drops or the app is closed - SendWorker sends it when it can. */
data class Outgoing(
    val localId: String,
    val file: String,
    val mime: String,
    val preview: String?,
    val replaces: Int?,
    val created: Long,
    var state: String,          // waiting / sending / reading / failed
    var serverId: Int? = null,
    var error: String? = null,
    var ocr: String? = null,    // null = not read on the phone yet
    var qr: String? = null,
    var summary: String = "",   // what the phone read, for the bubble
    var ocrPages: String = "",  // a PDF's pages as pictures, for reading on the phone ("|"-separated)
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("localId", localId); put("file", file); put("mime", mime); put("preview", preview)
        put("replaces", replaces); put("created", created); put("state", state); put("serverId", serverId)
        put("error", error); put("ocr", ocr); put("qr", qr); put("summary", summary); put("ocrPages", ocrPages)
    }

    companion object {
        fun from(j: JSONObject) = Outgoing(
            localId = j.getString("localId"), file = j.getString("file"), mime = j.getString("mime"),
            preview = j.optString("preview").takeIf { it.isNotEmpty() && it != "null" },
            replaces = if (j.isNull("replaces")) null else j.optInt("replaces"),
            created = j.optLong("created"), state = j.optString("state", "waiting"),
            serverId = if (j.isNull("serverId")) null else j.optInt("serverId"),
            error = if (j.isNull("error")) null else j.optString("error"),
            ocr = if (j.isNull("ocr")) null else j.optString("ocr"),
            qr = if (j.isNull("qr")) null else j.optString("qr"),
            summary = j.optString("summary"), ocrPages = j.optString("ocrPages"),
        )
    }
}

object Outbox {
    private lateinit var store: File
    lateinit var dir: File
        private set
    private val items = mutableListOf<Outgoing>()
    private val _changes = MutableStateFlow(0L)
    val changes: StateFlow<Long> = _changes

    fun init(context: Context) {
        dir = File(context.filesDir, "outbox").apply { mkdirs() }
        store = File(context.filesDir, "outbox.json")
        synchronized(this) {
            items.clear()
            runCatching {
                val array = JSONArray(store.readText())
                for (i in 0 until array.length()) items += Outgoing.from(array.getJSONObject(i))
            }
            // Anything cut off part-way is simply sent again.
            items.forEach { if (it.state == "sending") it.state = "waiting" }
        }
    }

    fun newFile(ext: String): File = File(dir, "${UUID.randomUUID()}.$ext")

    @Synchronized
    fun all(): List<Outgoing> = items.map { it.copy() }

    @Synchronized
    fun add(item: Outgoing) {
        items += item
        save()
    }

    @Synchronized
    fun update(localId: String, change: (Outgoing) -> Unit) {
        items.firstOrNull { it.localId == localId }?.let(change)
        save()
    }

    @Synchronized
    fun remove(localId: String) {
        items.firstOrNull { it.localId == localId }?.let { item ->
            File(item.file).delete()
            item.preview?.let { File(it).delete() }
            item.ocrPages.split("|").filter { it.isNotEmpty() }.forEach { File(it).delete() }
            items.remove(item)
        }
        save()
    }

    @Synchronized
    fun clear() {
        items.toList().forEach { remove(it.localId) }
    }

    private fun save() {
        val array = JSONArray()
        items.forEach { array.put(it.toJson()) }
        runCatching { store.writeText(array.toString()) }
        _changes.value = System.nanoTime()
    }
}
