package com.pixelwa.companion

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Detects 青山 fake-online (UI connected, no tun / Google blocked) and
 * reconnects: stop → force-stop → start FAB → confirm VPN dialog.
 */
object VpnWatchdog {
    private const val TAG = "WaVpnWatchdog"
    private const val COOLDOWN_MS = 90 * 1000L
    private const val HARD_BACKOFF_MS = 30 * 60 * 1000L
    private const val MAX_BURST = 4
    private const val BURST_WINDOW_MS = 60 * 60 * 1000L

    private val recovering = AtomicBoolean(false)

    fun shouldRecover(prefs: Prefs): Boolean {
        val now = System.currentTimeMillis()
        if (now - prefs.lastVpnRecoverAtMs < COOLDOWN_MS) {
            Log.i(TAG, "skip recover: cooldown")
            return false
        }
        val burst = prefs.vpnRecoverBurst
        val windowStart = prefs.vpnRecoverBurstWindowStartMs
        if (burst >= MAX_BURST && now - windowStart < BURST_WINDOW_MS) {
            if (now - prefs.lastVpnRecoverAtMs < HARD_BACKOFF_MS) {
                Log.w(TAG, "skip recover: burst $burst in last hour")
                return false
            }
        }
        return true
    }

    fun recover(context: Context, prefs: Prefs): Boolean {
        if (!recovering.compareAndSet(false, true)) {
            Log.i(TAG, "recover already running")
            return false
        }
        return try {
            Log.i(TAG, "recover start")
            val bound = waitFor(10_000L) { SendAccessibilityService.isBound() }
            if (!bound) {
                Log.e(TAG, "accessibility not bound; cannot recover")
                return false
            }
            markAttempt(prefs)

            openQingshan(context)
            val home = waitFor { SendAccessibilityService.hasQingshanConnectCard() }
            Log.i(TAG, "qingshan home visible=$home label=${SendAccessibilityService.qingshanConnectLabel()}")
            SendAccessibilityService.dismissPermissionDialogs()
            SendAccessibilityService.clickQingshanStopIfConnected()
            sleep(800)

            SendAccessibilityService.dismissPermissionDialogs()
            val started = SendAccessibilityService.clickQingshanStart()
            Log.i(TAG, "connect card clicked=$started")
            sleep(1200)
            SendAccessibilityService.confirmVpnPrepareDialog()

            val deadline = System.currentTimeMillis() + 25_000L
            while (System.currentTimeMillis() < deadline) {
                if (VpnHealth.hasVpnTransport(context)) break
                sleep(1000)
            }
            VpnHealth.invalidate()
            var snap = VpnHealth.probe(context, prefs, force = true)
            if (!snap.healthy) {
                Log.w(TAG, "still down after connect tap, try force-stop path")
                openAppInfo(context)
                sleep(2500)
                val stopped = SendAccessibilityService.forceStopCurrentApp()
                Log.i(TAG, "force stop clicked=$stopped")
                sleep(2000)
                openQingshan(context)
                waitFor { SendAccessibilityService.hasQingshanConnectCard() }
                SendAccessibilityService.dismissPermissionDialogs()
                val started2 = SendAccessibilityService.clickQingshanStart()
                Log.i(TAG, "connect retry clicked=$started2")
                sleep(1200)
                SendAccessibilityService.confirmVpnPrepareDialog()
                val deadline2 = System.currentTimeMillis() + 20_000L
                while (System.currentTimeMillis() < deadline2) {
                    if (VpnHealth.hasVpnTransport(context)) break
                    sleep(1000)
                }
                VpnHealth.invalidate()
                snap = VpnHealth.probe(context, prefs, force = true)
            }
            Log.i(TAG, "recover done ${snap.detail}")
            SendAccessibilityService.goHome()
            snap.healthy
        } catch (e: Exception) {
            Log.e(TAG, "recover failed", e)
            false
        } finally {
            recovering.set(false)
        }
    }

    private fun markAttempt(prefs: Prefs) {
        val now = System.currentTimeMillis()
        if (now - prefs.vpnRecoverBurstWindowStartMs > BURST_WINDOW_MS) {
            prefs.vpnRecoverBurstWindowStartMs = now
            prefs.vpnRecoverBurst = 0
        }
        prefs.vpnRecoverBurst = prefs.vpnRecoverBurst + 1
        prefs.lastVpnRecoverAtMs = now
    }

    private fun openQingshan(context: Context) {
        val launch = context.packageManager.getLaunchIntentForPackage(VpnHealth.QS_PACKAGE)
        if (launch != null) {
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            context.startActivity(launch)
            return
        }
        val intent = Intent().setClassName(
            VpnHealth.QS_PACKAGE,
            "com.wieifjyr.v.ui.MyMainActivity",
        )
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        context.startActivity(intent)
    }

    private fun waitFor(timeoutMs: Long = 8_000L, pred: () -> Boolean): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (pred()) return true
            sleep(400)
        }
        return pred()
    }

    private fun openAppInfo(context: Context) {
        val intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
            data = Uri.parse("package:${VpnHealth.QS_PACKAGE}")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
    }

    private fun sleep(ms: Long) {
        try {
            Thread.sleep(ms)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        }
    }
}
