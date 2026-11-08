package com.arfa.warehouse.sync

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.arfa.warehouse.WarehouseApp
import com.arfa.warehouse.data.Api
import com.arfa.warehouse.data.AuthNeeded
import com.arfa.warehouse.data.OutboxDao
import com.arfa.warehouse.data.Pending
import com.arfa.warehouse.push.Notifier
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Sends queued scans, oldest first, then their label photos. The server's
 * answer is final: a scan it refuses is marked so its line needs a scan
 * again, and the picker is told. With no connection it tries again later
 * (WorkManager waits for a network). */
class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val app = applicationContext as WarehouseApp
        return try {
            val done = Sync.run(app.api, app.db.outbox()) { serial, message ->
                Notifier.scanRefused(applicationContext, serial, message)
            }
            if (done) {
                app.events.tryEmit(WarehouseApp.Event.Synced)
                Result.success()
            } else Result.retry()
        } catch (e: AuthNeeded) {
            Result.success()          // waits for the next PIN unlock (which kicks this again)
        } catch (e: IOException) {
            Result.retry()
        }
    }

    companion object {
        private const val NAME = "warehouse-sync"

        fun kick(context: Context) {
            val request = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.LINEAR, 15, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(NAME, ExistingWorkPolicy.APPEND_OR_REPLACE, request)
        }

        fun queue(context: Context, item: Pending) {
            val app = context.applicationContext as WarehouseApp
            app.scope.launch(Dispatchers.IO) { app.db.outbox().add(item); kick(context) }
        }
    }
}

/** One pass over the outbox (kept apart from WorkManager so it is tested
 * against a fake server). False = the server is having trouble: try later. */
object Sync {
    suspend fun run(api: Api, outbox: OutboxDao, refused: (String, String) -> Unit): Boolean {
        for (item in outbox.queued()) {
            val reply = when (item.kind) {
                "pick" -> api.post("/orders/${item.parentId}/lines/${item.lineId}/picks", JSONObject()
                    .put("serial_no", item.serial).put("method", item.method)
                    .put("client_pick_id", item.clientId).put("allow_new", item.allowNew))
                "unit" -> api.post("/receipts/${item.parentId}/lines/${item.lineId}/units", JSONObject()
                    .put("serial_no", item.serial).put("method", item.method).put("client_unit_id", item.clientId))
                else -> api.post("/counts/${item.parentId}/scans", JSONObject()
                    .put("serial_no", item.serial).put("client_scan_id", item.clientId))
            }
            if (reply.code >= 500) return false
            val ok: Boolean
            val message: String
            val serverId: Long?
            when (item.kind) {
                "pick" -> {
                    val pick = reply.json.optJSONObject("pick")
                    ok = reply.ok && pick?.optBoolean("ok") == true
                    message = pick?.optString("message")?.takeIf { it.isNotEmpty() } ?: reply.error
                    serverId = pick?.optLong("id")
                }
                "unit" -> {
                    ok = reply.ok
                    message = if (reply.ok) reply.json.optString("message") else reply.error
                    serverId = reply.json.optJSONObject("unit")?.optLong("id")
                }
                else -> {
                    ok = reply.ok
                    message = if (reply.ok) reply.json.optString("message") else reply.error
                    serverId = null
                }
            }
            val after = QueueRules.afterServer(QueueRules.Pick(item.clientId, item.lineId, item.serial, item.state), ok, message)
            outbox.put(item.copy(state = after.state, message = after.message, serverId = serverId))
            if (!ok) refused(item.serial, message)
        }
        for (item in outbox.photosToSend()) {
            val file = item.photoPath?.let { File(it) }
            if (file == null || !file.exists()) {
                outbox.put(item.copy(photoSent = true))
                continue
            }
            val path = if (item.kind == "pick") "/picks/${item.serverId}/photo"
                       else "/receipts/${item.parentId}/units/${item.serverId}/photo"
            val reply = api.upload(path, file)
            if (reply.code >= 500) return false
            outbox.put(item.copy(photoSent = true))
            file.delete()
        }
        outbox.prune(System.currentTimeMillis() - TimeUnit.DAYS.toMillis(2))
        return true
    }
}
