package com.arfa.warehouse.data

import com.arfa.warehouse.BuildConfig
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/** The server's answer: the HTTP status and its JSON. */
data class Reply(val code: Int, val json: JSONObject) {
    val ok: Boolean get() = code in 200..299
    val error: String get() = json.optString("error", "Something went wrong (HTTP $code).")
    val signedOut: Boolean get() = code == 401 && json.optBoolean("signed_out")
    val pinRequired: Boolean get() = code == 401 && json.optBoolean("pin_required")
}

/** Thrown when the access token needs a PIN, or the phone was signed out. */
class AuthNeeded(val signedOut: Boolean) : IOException(if (signedOut) "signed out" else "PIN needed")

/** /api/wh/v1 over HTTPS with this phone's access token. Every call is
 * blocking - call it from a coroutine on Dispatchers.IO. IOException = no
 * connection. */
class Api(private val session: Session, base: String = BuildConfig.SERVER_BASE_URL) {

    private val root = base.trimEnd('/') + "/api/wh/v1"
    private val json = "application/json; charset=utf-8".toMediaType()
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    private fun request(path: String, auth: Boolean): Request.Builder {
        val b = Request.Builder().url(root + path)
            .header("X-App-Version", BuildConfig.VERSION_NAME)
            .header("Accept", "application/json")
        if (auth) session.accessToken?.let { b.header("Authorization", "Bearer $it") }
        return b
    }

    private fun send(req: Request, auth: Boolean): Reply {
        http.newCall(req).execute().use { res ->
            val text = res.body?.string().orEmpty()
            val body = runCatching { JSONObject(text) }.getOrElse { JSONObject().put("error", "The server sent an unexpected answer (HTTP ${res.code}).") }
            val reply = Reply(res.code, body)
            if (auth && reply.signedOut) throw AuthNeeded(true)
            if (auth && reply.pinRequired) throw AuthNeeded(false)
            return reply
        }
    }

    fun get(path: String, auth: Boolean = true): Reply = send(request(path, auth).get().build(), auth)

    fun post(path: String, body: JSONObject = JSONObject(), auth: Boolean = true): Reply =
        send(request(path, auth).post(body.toString().toRequestBody(json)).build(), auth)

    fun put(path: String, body: JSONObject, auth: Boolean = true): Reply =
        send(request(path, auth).put(body.toString().toRequestBody(json)).build(), auth)

    fun delete(path: String): Reply = send(request(path, true).delete().build(), true)

    fun upload(path: String, file: File, fields: Map<String, String> = emptyMap()): Reply {
        val form = MultipartBody.Builder().setType(MultipartBody.FORM)
        fields.forEach { (k, v) -> form.addFormDataPart(k, v) }
        val part: RequestBody = file.asRequestBody("image/jpeg".toMediaType())
        form.addFormDataPart("photo", file.name, part)
        return send(request(path, true).post(form.build()).build(), true)
    }
}
