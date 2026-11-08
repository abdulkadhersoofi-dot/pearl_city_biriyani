package com.arfa.warehouse.push

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.arfa.warehouse.MainActivity
import com.arfa.warehouse.R

/** The app's notification channels. "orders" rings like a chat message:
 * high importance (it pops up over whatever is open), the phone's
 * notification sound and a vibration - even with the app closed. */
object Notifier {
    const val ORDERS = "orders"
    const val EMI = "emi"
    const val UPDATES = "updates"
    const val SYNC = "sync"
    private val VIBRATE = longArrayOf(0, 300, 150, 300)

    fun channels(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        val sound = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
        val attrs = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_NOTIFICATION_COMMUNICATION_INSTANT)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
        manager.createNotificationChannel(
            NotificationChannel(ORDERS, context.getString(R.string.channel_orders), NotificationManager.IMPORTANCE_HIGH).apply {
                description = context.getString(R.string.channel_orders_desc)
                enableVibration(true)
                vibrationPattern = VIBRATE
                setSound(sound, attrs)
                enableLights(true)
                lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            })
        // EMI sale: the customer is waiting - rings on the alarm stream, so even a phone on silent sounds.
        val alarm = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
        manager.createNotificationChannel(
            NotificationChannel(EMI, "EMI sales - customer waiting", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "An EMI sale to lock now - rings even on silent"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 500, 200, 500, 200, 500)
                setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                    ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION), alarm)
                setBypassDnd(true)
                lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            })
        manager.createNotificationChannel(
            NotificationChannel(UPDATES, context.getString(R.string.channel_updates), NotificationManager.IMPORTANCE_MIN))
        manager.createNotificationChannel(
            NotificationChannel(SYNC, context.getString(R.string.channel_sync), NotificationManager.IMPORTANCE_DEFAULT))
    }

    private fun open(context: Context, orderId: Long?): PendingIntent {
        val intent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra(MainActivity.EXTRA_OPEN, "pipeline")
            orderId?.let { putExtra(MainActivity.EXTRA_ORDER, it) }
        }
        return PendingIntent.getActivity(context, (orderId ?: 0L).toInt(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    private fun allowed(context: Context): Boolean =
        Build.VERSION.SDK_INT < 33 || NotificationManagerCompat.from(context).areNotificationsEnabled()

    fun newOrder(context: Context, title: String, body: String, orderId: Long?, emi: Boolean = false) {
        if (!allowed(context)) return
        val n = NotificationCompat.Builder(context, if (emi) EMI else ORDERS)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle(title.ifEmpty { "New order" })
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setVibrate(VIBRATE)
            .setDefaults(NotificationCompat.DEFAULT_SOUND or NotificationCompat.DEFAULT_LIGHTS)
            .setAutoCancel(true)
            .setContentIntent(open(context, orderId))
            .build()
        try {
            NotificationManagerCompat.from(context).notify((orderId ?: System.currentTimeMillis()).toInt(), n)
        } catch (e: SecurityException) { /* notifications refused */ }
    }

    fun scanRefused(context: Context, serial: String, message: String) {
        if (!allowed(context)) return
        val n = NotificationCompat.Builder(context, SYNC)
            .setSmallIcon(android.R.drawable.stat_notify_error)
            .setContentTitle("Scan not accepted: $serial")
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText("$message The line needs a scan again."))
            .setAutoCancel(true)
            .setContentIntent(open(context, null))
            .build()
        try {
            NotificationManagerCompat.from(context).notify(serial.hashCode(), n)
        } catch (e: SecurityException) { }
    }
}
