package com.pixelwa.companion

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import kotlin.concurrent.thread

class WaNotificationListener : NotificationListenerService() {
    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn == null) return
        if (sbn.packageName != "com.whatsapp") return
        val prefs = Prefs(this)
        if (!prefs.automationEnabled) return

        val extras = sbn.notification.extras
        val title = extras.getString(Notification.EXTRA_TITLE)
        val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()
            ?: extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()
        if (text.isNullOrBlank()) return

        // Skip group summaries / own quiet noise
        if (sbn.notification.flags and Notification.FLAG_GROUP_SUMMARY != 0) return

        Log.i(TAG, "WA notif title=$title text=${text.take(80)}")
        if (!InboundDeduper.shouldReport(title, text)) {
            Log.i(TAG, "notif skip duplicate")
            return
        }
        thread {
            try {
                val api = OrchestratorApi(prefs)
                // title is often contact name; companion may not have phone — server matches cache by phone only.
                api.postInbound(from = title, body = text, receivedAt = sbn.postTime)
            } catch (e: Exception) {
                Log.e(TAG, "inbound post failed", e)
            }
        }
    }

    companion object {
        private const val TAG = "WaNotif"
    }
}
