package com.arfa.scan

import android.content.Context
import android.net.Uri
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Sends the bills waiting on the phone: reads each one on the phone (text
 * and QR), uploads it, then asks the server to read it - two at a time, as
 * the website does. Without a network it waits; a dropped connection is
 * retried with a growing pause. */
class SendWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (!Api.session.signedIn) return@withContext Result.success()
        var retry = false
        // 1. On the phone, then up to the server - one after another (the upload is the slow part).
        // (a bill the server refused waits for a tap on Retry - it would only be refused again)
        for (item in Outbox.all().filter { it.state == "waiting" }) {
            try {
                if (item.ocr == null) readOnPhone(item)
                val current = Outbox.all().firstOrNull { it.localId == item.localId } ?: continue
                Outbox.update(item.localId) { it.state = "sending"; it.error = null }
                val body = Api.upload(File(current.file), current.mime, current.ocr ?: "", current.qr, current.replaces)
                val id = body.getJSONObject("bill").getInt("id")
                Outbox.update(item.localId) { it.serverId = id; it.state = "reading" }
            } catch (e: ApiError) {
                if (e.signedOut) return@withContext Result.failure()
                Outbox.update(item.localId) { it.state = "failed"; it.error = e.message }
            } catch (e: IOException) {
                Outbox.update(item.localId) { it.state = "waiting" }
                retry = true
            }
        }
        // 2. Read on the server, two at a time.
        val gate = Semaphore(2)
        coroutineScope {
            Outbox.all().filter { it.state == "reading" && it.serverId != null }.map { item ->
                async {
                    gate.withPermit {
                        try {
                            Api.post("/bills/${item.serverId}/read")
                            Outbox.remove(item.localId)   // the chat shows the server's bill from now on
                        } catch (e: ApiError) {
                            // Read or not (a daily limit, the service down), the bill is on the server now.
                            Outbox.remove(item.localId)
                        } catch (e: IOException) {
                            retry = true
                        }
                    }
                }
            }.awaitAll()
        }
        if (retry) Result.retry() else Result.success()
    }

    private suspend fun readOnPhone(item: Outgoing) {
        val pages = if (item.mime == "application/pdf")
            item.ocrPages.split("|").filter { it.isNotEmpty() }.map { Uri.fromFile(File(it)) }
        else listOf(Uri.fromFile(File(item.file)))
        val read = runCatching { PhoneReader.read(applicationContext, pages) }.getOrNull()
            ?: PhoneReader.Read("", null)
        Outbox.update(item.localId) {
            it.ocr = read.text
            it.qr = read.qr
            it.summary = PhoneReader.summary(read)
        }
        // the page pictures were only for reading on the phone
        item.ocrPages.split("|").filter { it.isNotEmpty() }.forEach { File(it).delete() }
        Outbox.update(item.localId) { it.ocrPages = "" }
    }

    companion object {
        fun start(context: Context) {
            val request = OneTimeWorkRequestBuilder<SendWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 10, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork("send-bills", ExistingWorkPolicy.APPEND_OR_REPLACE, request)
        }
    }
}
