package com.pixelwa.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
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
    private lateinit var screenWake: ScreenWake

    private val tick = object : Runnable {
        override fun run() {
            if (!running) return
            lastTickAtMs = System.currentTimeMillis()
            if (prefs.automationEnabled) {
                thread {
                    ServiceWatchdog.ensureAlive(this@PollingForegroundService)
                    pollOnce()
                }
            }
            handler.postDelayed(this, POLL_MS)
        }
    }

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        api = OrchestratorApi(prefs)
        screenWake = ScreenWake(this)
        createChannel()
        instanceRunning = true
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val promoted = promoteToForeground()
        if (!promoted) {
            Log.e(TAG, "startForeground failed; stop and wait for watchdog/user")
            instanceRunning = false
            stopSelf()
            return START_NOT_STICKY
        }
        lastTickAtMs = System.currentTimeMillis()
        instanceRunning = true
        if (!running) {
            running = true
            handler.post(tick)
        }
        return START_STICKY
    }

    /**
     * Android 15+: if a time-limited FGS type is ever used again, stop cleanly
     * instead of crashing with ForegroundServiceDidNotStopInTimeException.
     */
    override fun onTimeout(startId: Int, fgsType: Int) {
        Log.w(TAG, "onTimeout startId=$startId fgsType=$fgsType — stopSelf")
        running = false
        handler.removeCallbacks(tick)
        instanceRunning = false
        stopSelf()
    }

    private fun promoteToForeground(): Boolean {
        val notification = buildNotification(prefs.automationEnabled, vpnOk = true)
        return try {
            if (Build.VERSION.SDK_INT >= 34) {
                ServiceCompat.startForeground(
                    this,
                    NOTIF_ID,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
                )
            } else {
                startForeground(NOTIF_ID, notification)
            }
            true
        } catch (e: Exception) {
            Log.e(TAG, "startForeground failed", e)
            false
        }
    }

    private fun maybeWatchVpn() {
        val extras = ServiceWatchdog.healthExtras(this, prefs)
        if (!prefs.vpnWatchdogEnabled) {
            api.heartbeat(extras = extras)
            return
        }
        val snap = VpnHealth.probe(this, prefs)
        api.heartbeat(snap, extras)
        if (snap.healthy) return
        if (!VpnWatchdog.shouldRecover(prefs)) return
        Log.w(TAG, "VPN unhealthy, recovering: ${snap.detail}")
        screenWake.acquire(timeoutMs = 90_000L)
        val ok = VpnWatchdog.recover(this, prefs)
        VpnHealth.invalidate()
        val after = VpnHealth.probe(this, prefs, force = true).copy(recovered = ok)
        api.heartbeat(after, ServiceWatchdog.healthExtras(this, prefs))
        refreshNotification(ok)
        if (!after.healthy) {
            AlertSms.maybeSend(
                this,
                prefs,
                "VPN recover failed: ${after.detail}",
            )
        }
    }

    private fun refreshNotification(vpnOk: Boolean) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIF_ID, buildNotification(prefs.automationEnabled, vpnOk))
    }

    override fun onDestroy() {
        running = false
        instanceRunning = false
        handler.removeCallbacks(tick)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun pollOnce() {
        if (!busy.compareAndSet(false, true)) return
        try {
            maybeWatchVpn()
            val job = api.nextJob() ?: return
            Log.i(TAG, "got job ${job.id} type=${job.jobType} -> ${job.phone}")
            val wakeMs = when {
                job.isProbe -> 60_000L
                job.hasMedia -> 600_000L
                else -> 120_000L
            }
            screenWake.acquire(timeoutMs = wakeMs)
            try {
                Thread.sleep(800)
            } catch (_: InterruptedException) {
                // continue
            }
            if (job.isProbe) {
                val (hasWa, status, detail) = probeWhatsApp(job)
                api.reportResult(
                    job.id,
                    ok = status != "error",
                    error = if (status == "error") detail else null,
                    hasWhatsapp = hasWa,
                    probeStatus = status,
                    detail = detail,
                )
                SendAccessibilityService.leaveChatToList()
                return
            }
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
            SendAccessibilityService.leaveChatToList()
        } catch (e: Exception) {
            Log.e(TAG, "poll failed", e)
            SendAccessibilityService.leaveChatToList()
        } finally {
            screenWake.release()
            busy.set(false)
        }
    }

    /**
     * Open api.whatsapp.com/send for [job.phone], detect registration, never send.
     * @return Triple(hasWhatsapp, status yes|no|unknown|error, detail)
     */
    private fun probeWhatsApp(job: WaJob): Triple<Boolean?, String, String?> {
        val digits = job.phone.filter { it.isDigit() }
        if (digits.length < 8) {
            return Triple(null, "error", "invalid phone digits")
        }
        val uri = "https://api.whatsapp.com/send?phone=$digits".toUri()
        val latch = CountDownLatch(1)
        var hasWa: Boolean? = null
        var status = "unknown"
        var detail: String? = null
        ProbeCoordinator.pendingJobId = job.id
        ProbeCoordinator.callback = { hw, st, det ->
            hasWa = hw
            status = st
            detail = det
            latch.countDown()
        }
        SendAccessibilityService.armForProbe()
        handler.post {
            screenWake.acquire(timeoutMs = 60_000L)
            try {
                startActivity(
                    Intent(Intent.ACTION_VIEW, uri).apply {
                        setPackage("com.whatsapp")
                        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    },
                )
            } catch (e: Exception) {
                Log.e(TAG, "probe open whatsapp failed", e)
                ProbeCoordinator.onResult(null, "error", "无法打开 WhatsApp：${e.message}")
            }
        }
        val finished = latch.await(25, TimeUnit.SECONDS)
        if (!finished) {
            ProbeCoordinator.onResult(null, "unknown", "probe timeout 25s")
            latch.await(1, TimeUnit.SECONDS)
        }
        Log.i(TAG, "probe done phone=$digits status=$status has=$hasWa detail=$detail")
        return Triple(hasWa, status, detail)
    }

    private fun sendViaWhatsApp(job: WaJob): Pair<Boolean, String?> {
        if (job.hasMedia) {
            return sendMediaViaWhatsApp(job)
        }
        return sendTextViaWhatsApp(job)
    }

    private fun sendTextViaWhatsApp(job: WaJob): Pair<Boolean, String?> {
        val digits = job.phone.filter { it.isDigit() }
        val encoded = URLEncoder.encode(job.text, StandardCharsets.UTF_8.toString())
        // api.whatsapp.com is more reliable for numbers not yet in the chat list
        val uri = "https://api.whatsapp.com/send?phone=$digits&text=$encoded".toUri()
        return launchAndAwaitSend(
            job = job,
            pendingText = job.text,
            timeoutSec = 35,
        ) {
            Intent(Intent.ACTION_VIEW, uri).apply {
                setPackage("com.whatsapp")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
        }
    }

    private fun sendMediaViaWhatsApp(job: WaJob): Pair<Boolean, String?> {
        val mediaUrl = job.mediaUrl ?: return false to "缺少 mediaUrl"
        val mediaType = job.mediaType ?: return false to "缺少 mediaType"
        Log.i(TAG, "download media type=$mediaType url=${mediaUrl.take(80)}")
        val file = MediaHelper.downloadToCache(this, mediaUrl, mediaType)
            ?: return false to "媒体下载失败"
        val digits = job.phone.filter { it.isDigit() }
        val mime = MediaHelper.mimeFor(mediaType, file.name)
        val contentUri = MediaHelper.fileProviderUri(this, file)
        return launchAndAwaitSend(
            job = job,
            pendingText = job.text,
            timeoutSec = 180,
        ) {
            grantUriPermission(
                "com.whatsapp",
                contentUri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION,
            )
            Intent(Intent.ACTION_SEND).apply {
                type = mime
                putExtra(Intent.EXTRA_STREAM, contentUri)
                if (job.text.isNotBlank()) {
                    putExtra(Intent.EXTRA_TEXT, job.text)
                }
                // Opens chat for this number when WhatsApp honors the undocumented jid extra.
                putExtra("jid", "$digits@s.whatsapp.net")
                setPackage("com.whatsapp")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
        }
    }

    private fun launchAndAwaitSend(
        job: WaJob,
        pendingText: String,
        timeoutSec: Long,
        intentFactory: () -> Intent,
    ): Pair<Boolean, String?> {
        val latch = CountDownLatch(1)
        var result = false
        var err: String? = null
        SendCoordinator.pendingJobId = job.id
        SendCoordinator.pendingText = pendingText
        SendCoordinator.pendingMedia = job.hasMedia
        SendCoordinator.callback = { ok, error ->
            result = ok
            err = error
            latch.countDown()
        }
        SendAccessibilityService.armForSend()
        handler.post {
            screenWake.acquire(timeoutMs = if (job.hasMedia) 600_000L else 120_000L)
            try {
                startActivity(intentFactory())
            } catch (e: Exception) {
                Log.e(TAG, "open whatsapp failed", e)
                SendCoordinator.onSendAttempted(false, "无法打开 WhatsApp：${e.message}")
            }
        }
        val finished = latch.await(timeoutSec, TimeUnit.SECONDS)
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
            // Media-only: a11y may have clicked send without a text bubble to match.
            if (job.hasMedia && want.isEmpty()) {
                Log.w(TAG, "media send timeout without caption; treating as UNCERTAIN")
            }
            val msg = "UNCERTAIN:发送超时（${timeoutSec}s 未确认，请勿直接重试）"
            SendCoordinator.onSendAttempted(false, msg)
            return false to msg
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

    private fun buildNotification(on: Boolean, vpnOk: Boolean = true): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        val text = when {
            !on -> "OFF"
            vpnOk -> "ON — polling jobs"
            else -> "ON — VPN recovering"
        }
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(text)
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

        @Volatile
        private var instanceRunning: Boolean = false

        @Volatile
        var lastTickAtMs: Long = 0L
            private set

        fun isAliveRecently(maxAgeMs: Long = 45_000L): Boolean {
            if (!instanceRunning) return false
            val last = lastTickAtMs
            if (last <= 0L) return false
            return System.currentTimeMillis() - last <= maxAgeMs
        }

        /** Start or revive the polling FGS; returns false if the system blocked startForeground. */
        fun ensureStarted(context: Context): Boolean {
            val prefs = Prefs(context)
            if (!prefs.automationEnabled) return false
            if (isAliveRecently()) return true
            return try {
                ContextCompat.startForegroundService(
                    context.applicationContext,
                    Intent(context.applicationContext, PollingForegroundService::class.java),
                )
                true
            } catch (e: Exception) {
                Log.e(TAG, "ensureStarted failed", e)
                false
            }
        }
    }
}
