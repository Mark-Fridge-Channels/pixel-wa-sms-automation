package com.pixelwa.companion

import android.app.Notification
import android.app.PendingIntent
import android.graphics.Bitmap
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import java.io.File
import kotlin.concurrent.thread

class WaNotificationListener : NotificationListenerService() {
    private val healthHandler = Handler(Looper.getMainLooper())
    private val healthTick = object : Runnable {
        override fun run() {
            try {
                ServiceWatchdog.ensureAlive(this@WaNotificationListener)
            } catch (e: Exception) {
                Log.w(TAG, "health tick failed", e)
            }
            healthHandler.postDelayed(this, HEALTH_INTERVAL_MS)
        }
    }

    override fun onListenerConnected() {
        super.onListenerConnected()
        Log.i(TAG, "notification listener connected")
        healthHandler.removeCallbacks(healthTick)
        healthHandler.post(healthTick)
    }

    override fun onListenerDisconnected() {
        healthHandler.removeCallbacks(healthTick)
        super.onListenerDisconnected()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn == null) return
        // Any notification is a chance to revive polling if it died.
        if (Prefs(this).automationEnabled) {
            ServiceWatchdog.ensureAlive(this)
        }
        if (sbn.packageName != "com.whatsapp") return
        val prefs = Prefs(this)
        if (!prefs.automationEnabled) return
        if (sbn.notification.flags and Notification.FLAG_GROUP_SUMMARY != 0) return

        val extras = sbn.notification.extras
        val title = extras.getString(Notification.EXTRA_TITLE)
            ?: extras.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE)?.toString()
        val text = extractNotificationText(extras) ?: return
        Log.i(TAG, "WA notif title=$title text=${text.take(80)}")

        if (!InboundDeduper.shouldReport(title, text)) {
            Log.i(TAG, "notif skip duplicate")
            return
        }

        val hasPicture = hasRealMediaPicture(sbn.notification)
        val contentIntent = sbn.notification.contentIntent
        thread {
            try {
                handleInbound(prefs, title, text, sbn.postTime, hasPicture, contentIntent)
            } catch (e: Exception) {
                Log.e(TAG, "inbound post failed", e)
            }
        }
    }

    private fun extractNotificationText(extras: android.os.Bundle): String? {
        val primary = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()
            ?: extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()
            ?: extras.getCharSequence(Notification.EXTRA_INFO_TEXT)?.toString()
            ?: extras.getCharSequence(Notification.EXTRA_SUB_TEXT)?.toString()
        if (!primary.isNullOrBlank()) return primary

        if (Build.VERSION.SDK_INT >= 24) {
            @Suppress("UNCHECKED_CAST")
            val lines = extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)
            if (lines != null) {
                for (i in lines.indices.reversed()) {
                    val s = lines[i]?.toString()?.trim().orEmpty()
                    if (s.isNotEmpty()) return s
                }
            }
            val messages = extras.getParcelableArray(Notification.EXTRA_MESSAGES)
            if (messages != null) {
                for (i in messages.indices.reversed()) {
                    val m = messages[i]
                    if (m is android.os.Bundle) {
                        val s = m.getCharSequence("text")?.toString()?.trim().orEmpty()
                        if (s.isNotEmpty()) return s
                    }
                }
            }
        }
        return null
    }

    /**
     * Only [Notification.EXTRA_PICTURE] indicates a media thumbnail.
     * Never use largeIcon — on WhatsApp that is the contact/group avatar.
     */
    private fun hasRealMediaPicture(notification: Notification): Boolean {
        val extras = notification.extras
        @Suppress("DEPRECATION")
        val picture = extras.getParcelable<Bitmap>(Notification.EXTRA_PICTURE)
        return picture != null && !picture.isRecycled && picture.width >= 64 && picture.height >= 64
    }

    private fun handleInbound(
        prefs: Prefs,
        title: String?,
        text: String,
        postTime: Long,
        hasPicture: Boolean,
        contentIntent: PendingIntent?,
    ) {
        val mediaType = MediaHelper.detectMediaTypeFromNotification(text)
            ?: if (hasPicture) "image" else null

        if (mediaType != null) {
            val wake = ScreenWake(this)
            wake.acquire(timeoutMs = 120_000L)
            var openedChat = false
            try {
                openedChat = openChatToTriggerDownload(contentIntent)
                val file = waitForMediaFile(mediaType)
                if (file != null) {
                    Log.i(
                        TAG,
                        "media file ready type=$mediaType path=${file.absolutePath} size=${file.length()}",
                    )
                    val ok = MediaHelper.uploadInboundMedia(
                        prefs,
                        file,
                        from = title,
                        caption = text,
                        mediaType = mediaType,
                        receivedAt = postTime,
                    )
                    if (ok) {
                        Log.i(TAG, "media upload ok type=$mediaType")
                        return
                    }
                    Log.w(TAG, "media upload failed; fallback text")
                } else {
                    Log.w(
                        TAG,
                        "no media file for type=$mediaType openedChat=$openedChat; fallback text only",
                    )
                }
            } finally {
                if (openedChat && !SendAccessibilityService.isSending()) {
                    SendAccessibilityService.leaveChatToList()
                }
                wake.release()
            }
        }
        OrchestratorApi(prefs).postInbound(from = title, body = text, receivedAt = postTime)
    }

    /**
     * Fire the notification's contentIntent so WhatsApp opens the chat and downloads media.
     * Skip while an outbound send is in progress to avoid UI fights.
     */
    private fun openChatToTriggerDownload(contentIntent: PendingIntent?): Boolean {
        if (contentIntent == null) {
            Log.w(TAG, "no contentIntent to open chat")
            return false
        }
        if (SendAccessibilityService.isSending()) {
            Log.i(TAG, "skip open chat: outbound send in progress")
            return false
        }
        return try {
            contentIntent.send()
            Log.i(TAG, "opened chat via notification contentIntent")
            // Give WA time to resume chat + start download before first disk poll.
            Thread.sleep(3_500)
            true
        } catch (e: Exception) {
            Log.w(TAG, "contentIntent.send failed", e)
            false
        }
    }

    /** Poll disk + MediaStore while WhatsApp finishes downloading the attachment. */
    private fun waitForMediaFile(mediaType: String): File? {
        val deadline = System.currentTimeMillis() + 75_000L
        var attempt = 0
        while (System.currentTimeMillis() < deadline) {
            attempt++
            val ageMs = 300_000L
            val file = MediaHelper.findRecentWhatsAppMedia(mediaType, maxAgeMs = ageMs)
                ?: MediaHelper.findRecentMediaStore(this, mediaType, maxAgeMs = ageMs)
            if (file != null && MediaHelper.isPlausibleInboundMedia(file, mediaType)) {
                Log.i(TAG, "media found attempt=$attempt path=${file.absolutePath}")
                return file
            }
            try {
                Thread.sleep(2_500)
            } catch (_: InterruptedException) {
                break
            }
        }
        return null
    }

    companion object {
        private const val TAG = "WaNotif"
        private const val HEALTH_INTERVAL_MS = 45_000L
    }
}
