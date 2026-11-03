package com.arfa.scan

import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/** The server answered with an error: its own message, the HTTP status and
 * the body (code "difference" = the total is off, post anyway?). */
class ApiError(message: String, val status: Int, val body: JSONObject?) : Exception(message) {
    val code: String get() = body?.optString("code") ?: ""
    val signedOut: Boolean get() = status == 401
}

/** The ARFA server's Scan app API (app/scan/api.py), over one shared
 * HTTP client that adds the sign-in token to every request to the server -
 * the pictures included (Coil uses the same client). */
object Api {
    const val BASE = "/api/scan/v1"
    lateinit var session: Session
    private val JSON = "application/json; charset=utf-8".toMediaType()

    val http: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)   // reading a bill takes a few seconds
            .writeTimeout(120, TimeUnit.SECONDS)
            .addInterceptor { chain ->
                val request = chain.request()
                val token = session.token
                val ours = request.url.host == session.server.toHttpUrlOrNull()?.host
                val builder = request.newBuilder().header("User-Agent", "ARFAScan/${BuildConfig.VERSION_NAME} (Android)")
                if (ours && token != null && request.header("Authorization") == null) {
                    builder.header("Authorization", "Bearer $token")
                }
                chain.proceed(builder.build())
            }
            .build()
    }

    fun url(path: String): String = if (path.startsWith("http")) path else session.server.trimEnd('/') + path

    fun get(path: String): JSONObject = run(Request.Builder().url(url(BASE + path)).get().build())

    fun post(path: String, body: JSONObject = JSONObject()): JSONObject =
        run(Request.Builder().url(url(BASE + path)).post(body.toString().toRequestBody(JSON)).build())

    /** A bill, with what the phone read on it. */
    fun upload(file: File, mime: String, ocrText: String, qr: String?, replaces: Int?): JSONObject {
        val form = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("file", file.name, file.asRequestBody(mime.toMediaType()))
            .addFormDataPart("ocr_text", ocrText)
        if (!qr.isNullOrEmpty()) form.addFormDataPart("qr", qr)
        if (replaces != null) form.addFormDataPart("replaces", replaces.toString())
        return run(Request.Builder().url(url("$BASE/bills")).post(form.build()).build())
    }

    fun bytes(path: String): ByteArray {
        http.newCall(Request.Builder().url(url(path)).get().build()).execute().use { response ->
            if (!response.isSuccessful) throw ApiError("The picture didn't load (${response.code}).", response.code, null)
            return response.body?.bytes() ?: ByteArray(0)
        }
    }

    private fun run(request: Request): JSONObject {
        http.newCall(request).execute().use { response ->
            val text = response.body?.string() ?: ""
            val json = runCatching { JSONObject(text) }.getOrNull()
            if (!response.isSuccessful) {
                val message = json?.optString("error")?.takeIf { it.isNotEmpty() }
                    ?: "The server answered ${response.code} - try again."
                throw ApiError(message, response.code, json)
            }
            return json ?: JSONObject()
        }
    }

}
