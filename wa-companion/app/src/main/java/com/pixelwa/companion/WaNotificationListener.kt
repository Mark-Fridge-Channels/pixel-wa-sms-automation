package com.pixelwa.companion

import android.app.Notification
import android.graphics.Bitmap
import android.os.Build
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import java.io.File
import java.io.FileOutputStream
import kotlin.concurrent.thread

class WaNotificationListener : NotificationListenerService() {
    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn == null) return
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

        val previewBmp = extractPreviewBitmap(sbn.notification)
        thread {
            try {
                handleInbound(prefs, title, text, sbn.postTime, previewBmp)
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

    private fun extractPreviewBitmap(notification: Notification): Bitmap? {
        val extras = notification.extras
        @Suppress("DEPRECATION")
        val picture = extras.getParcelable<Bitmap>(Notification.EXTRA_PICTURE)
        if (picture != null && !picture.isRecycled && picture.width > 32) return picture
        val large = notification.getLargeIcon()
        if (large != null) {
            return try {
                large.loadDrawable(this)?.let { d ->
                    val w = (d.intrinsicWidth).coerceAtLeast(1)
                    val h = (d.intrinsicHeight).coerceAtLeast(1)
                    if (w < 64 || h < 64) return null
                    val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
                    val canvas = android.graphics.Canvas(bmp)
                    d.setBounds(0, 0, w, h)
                    d.draw(canvas)
                    bmp
                }
            } catch (e: Exception) {
                Log.w(TAG, "largeIcon decode failed", e)
                null
            }
        }
        return null
    }

    private fun handleInbound(
        prefs: Prefs,
        title: String?,
        text: String,
        postTime: Long,
        previewBmp: Bitmap?,
    ) {
        val mediaType = MediaHelper.detectMediaTypeFromNotification(text)
        if (mediaType != null) {
            var file = waitForMediaFile(mediaType)
            if (file == null && mediaType == "image" && previewBmp != null) {
                file = saveBitmapPreview(previewBmp)
                if (file != null) {
                    Log.i(TAG, "using notification preview bitmap size=${file.length()}")
                }
            }
            if (file != null) {
                Log.i(TAG, "media file ready type=$mediaType path=${file.absolutePath} size=${file.length()}")
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
                Log.w(TAG, "no media file for type=$mediaType; fallback text")
            }
        }
        OrchestratorApi(prefs).postInbound(from = title, body = text, receivedAt = postTime)
    }

    /** Poll disk + MediaStore while WhatsApp finishes downloading the attachment. */
    private fun waitForMediaFile(mediaType: String): File? {
        val deadline = System.currentTimeMillis() + 45_000L
        var attempt = 0
        while (System.currentTimeMillis() < deadline) {
            attempt++
            val ageMs = 180_000L
            val file = MediaHelper.findRecentWhatsAppMedia(mediaType, maxAgeMs = ageMs)
                ?: MediaHelper.findRecentMediaStore(this, mediaType, maxAgeMs = ageMs)
            if (file != null) {
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

    private fun saveBitmapPreview(bmp: Bitmap): File? {
        return try {
            val out = File(cacheDir, "notif_preview_${System.currentTimeMillis()}.jpg")
            FileOutputStream(out).use { fos ->
                bmp.compress(Bitmap.CompressFormat.JPEG, 90, fos)
            }
            if (out.length() > 64) out else null
        } catch (e: Exception) {
            Log.e(TAG, "save preview failed", e)
            null
        }
    }

    companion object {
        private const val TAG = "WaNotif"
    }
}
