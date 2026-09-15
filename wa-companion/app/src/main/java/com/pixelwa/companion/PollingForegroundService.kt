package com.pixelwa.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.net.toUri
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

class PollingForegroundService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private var running = false
    private val busy = AtomicBoolean(false)
    private lateinit var prefs: Prefs
    private lateinit var api: OrchestratorApi

    private val tick = object : Runnable {
        override fun run() {
            if (!running) return
            if (prefs.automationEnabled) {
                thread { pollOnce() }
            }
            handler.postDelayed(this, POLL_MS)
        }
    }

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        api = OrchestratorApi(prefs)
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIF_ID, buildNotification(prefs.automationEnabled))
        if (!running) {
            running = true
            handler.post(tick)
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        handler.removeCallbacks(tick)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun pollOnce() {
        if (!busy.compareAndSet(false, true)) return
        try {
            api.heartbeat()
            val job = api.nextJob() ?: return
            Log.i(TAG, "got job ${job.id} -> ${job.phone}")
            val (ok, error) = sendViaWhatsApp(job)
            api.reportResult(
                job.id,
                ok,
                if (ok) {
                    null
                } else {
                    error ?: "UNCERTAIN:发送未确认（无障碍未点到发送或超时）"
                },
            )
            if (ok) {
                dwellAndScrapeReplies(job.phone, job.text)
            }
            // Always leave the open chat so the phone is not stuck on WhatsApp compose.
            SendAccessibilityService.leaveChatToList()
        } catch (e: Exception) {
            Log.e(TAG, "poll failed", e)
            SendAccessibilityService.leaveChatToList()
        } finally {
            busy.set(false)
        }
    }

    private fun sendViaWhatsApp(job: WaJob): Pair<Boolean, String?> {
        val digits = job.phone.filter { it.isDigit() }
        val encoded = URLEncoder.encode(job.text, StandardCharsets.UTF_8.toString())
        // api.whatsapp.com is more reliable for numbers not yet in the chat list
        val uri = "https://api.whatsapp.com/send?phone=$digits&text=$encoded".toUri()
        val latch = CountDownLatch(1)
        var result = false
        var err: String? = null
        SendCoordinator.pendingJobId = job.id
        SendCoordinator.pendingText = job.text
        SendCoordinator.callback = { ok, error ->
            result = ok
            err = error
            latch.countDown()
        }
        SendAccessibilityService.armForSend()
        handler.post {
            val intent = Intent(Intent.ACTION_VIEW, uri).apply {
                setPackage("com.whatsapp")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            try {
                startActivity(intent)
            } catch (e: Exception) {
                Log.e(TAG, "open whatsapp failed", e)
                SendCoordinator.onSendAttempted(false, "无法打开 WhatsApp：${e.message}")
            }
        }
        val finished = latch.await(35, TimeUnit.SECONDS)
        if (!finished) {
            val want = SendCoordinator.pendingText?.trim().orEmpty()
            if (want.isNotEmpty()) {
                val bubbles = SendAccessibilityService.snapshotMessages()
                val seen = bubbles.any { bubble ->
                    val b = bubble.trim()
                    b == want || b.contains(want) || want.contains(b.take(40))
                }
                if (seen) {
                    Log.i(TAG, "timeout but message bubble found; treat as success")
                    SendCoordinator.onSendAttempted(true, null)
                    return true to null
                }
            }
            SendCoordinator.onSendAttempted(
                false,
                "UNCERTAIN:发送超时（35s 未确认，请勿直接重试）",
            )
            return false to "UNCERTAIN:发送超时（35s 未确认，请勿直接重试）"
        }
        return result to err
    }

    /**
     * Stay in the open chat briefly and scrape new bubbles.
     * Reply time = device clock when the bubble is first seen (not UI clock text).
     */
    private fun dwellAndScrapeReplies(phone: String, sentText: String) {
        val known = SendAccessibilityService.snapshotMessages().toMutableSet()
        known.add(sentText.trim())
        val deadline = System.currentTimeMillis() + DWELL_MS
        Log.i(TAG, "dwell scrape start phone=$phone known=${known.size}")
        while (System.currentTimeMillis() < deadline) {
            try {
                Thread.sleep(SCRAPE_INTERVAL_MS)
            } catch (_: InterruptedException) {
                break
            }
            val nowVisible = SendAccessibilityService.snapshotMessages()
            for (text in nowVisible) {
                if (text in known) continue
                known.add(text)
                if (!InboundDeduper.shouldReport(phone, text)) {
                    Log.i(TAG, "scrape skip duplicate: ${text.take(40)}")
                    continue
                }
                val receivedAt = System.currentTimeMillis()
                Log.i(TAG, "scrape inbound: ${text.take(60)}")
                try {
                    api.postInbound(from = phone, body = text, receivedAt = receivedAt)
                } catch (e: Exception) {
                    Log.e(TAG, "scrape inbound post failed", e)
                }
            }
        }
        Log.i(TAG, "dwell scrape end")
    }

    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notif_channel),
                NotificationManager.IMPORTANCE_LOW
            )
        )
    }

    private fun buildNotification(on: Boolean): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(if (on) "ON — polling jobs" else "OFF")
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val TAG = "WaPoll"
        private const val CHANNEL_ID = "wa_companion"
        private const val NOTIF_ID = 42
        private const val POLL_MS = 15_000L
        // Short dwell for bubble scrape; inbound also covered by notification listener.
        private const val DWELL_MS = 8_000L
        private const val SCRAPE_INTERVAL_MS = 1_500L
    }
}
