package com.arfa.warehouse.push

import com.arfa.warehouse.WarehouseApp
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/** Pushes from the server (app/warehouse/notify.py): data messages at high
 * priority, so they reach the app even when it's closed. A new order rings
 * (Notifier.newOrder); every push refreshes an open pipeline. */
class PushService : FirebaseMessagingService() {

    override fun onMessageReceived(message: RemoteMessage) {
        val data = message.data
        val app = application as WarehouseApp
        if (!app.session.signedIn) return
        if (data["silent"] == "0") {
            Notifier.newOrder(this, data["title"].orEmpty(), data["body"].orEmpty(), data["order_id"]?.toLongOrNull())
        }
        app.events.tryEmit(WarehouseApp.Event.Refresh)
    }

    override fun onNewToken(token: String) {
        val app = application as WarehouseApp
        app.session.fcmToken = token
        app.sendPushToken()
    }
}
