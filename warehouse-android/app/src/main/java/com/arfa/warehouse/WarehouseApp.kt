package com.arfa.warehouse

import android.app.Application
import com.arfa.warehouse.data.Api
import com.arfa.warehouse.data.AppDb
import com.arfa.warehouse.data.Session
import com.arfa.warehouse.push.Notifier
import com.arfa.warehouse.sync.SyncWorker
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.launch
import org.json.JSONObject

/** The app's one session, API client and offline store; pushes are set up
 * here with the Firebase settings the server hands out (/app/config), so
 * the build carries none. Without them the app keeps to polling. */
class WarehouseApp : Application() {

    enum class Event { Refresh, Synced }

    lateinit var session: Session
        private set
    lateinit var api: Api
        private set
    val db: AppDb by lazy { AppDb.get(this) }
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    val events = MutableSharedFlow<Event>(extraBufferCapacity = 8, onBufferOverflow = BufferOverflow.DROP_OLDEST)

    override fun onCreate() {
        super.onCreate()
        session = Session(this)
        api = Api(session)
        Notifier.channels(this)
        session.pushConfig?.let { startPush(it) }
        scope.launch(Dispatchers.IO) { refreshPushConfig() }
        if (session.signedIn) SyncWorker.kick(this)
    }

    private fun refreshPushConfig() {
        try {
            val reply = api.get("/app/config", auth = false)
            val push = reply.json.optJSONObject("push")
            if (reply.ok && push != null && push.toString() != session.pushConfig?.toString()) {
                session.pushConfig = push
                scope.launch { startPush(push) }
            }
        } catch (e: Exception) { /* offline: try at the next start */ }
    }

    @Synchronized
    private fun startPush(cfg: JSONObject) {
        try {
            if (FirebaseApp.getApps(this).isEmpty()) {
                val options = FirebaseOptions.Builder()
                    .setApplicationId(cfg.getString("app_id"))
                    .setApiKey(cfg.getString("api_key"))
                    .setProjectId(cfg.getString("project_id"))
                    .setGcmSenderId(cfg.getString("sender_id"))
                    .build()
                FirebaseApp.initializeApp(this, options)
            }
            FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
                session.fcmToken = token
                sendPushToken()
            }
        } catch (e: Exception) { /* bad settings: polling only */ }
    }

    /** Tell the server this phone's push token (once per token, signed in). */
    fun sendPushToken() {
        val token = session.fcmToken ?: return
        if (!session.signedIn || session.accessToken == null || token == session.fcmTokenSent) return
        scope.launch(Dispatchers.IO) {
            try {
                if (api.put("/auth/fcm-token", JSONObject().put("token", token)).ok) session.fcmTokenSent = token
            } catch (e: Exception) { /* next unlock tries again */ }
        }
    }
}
