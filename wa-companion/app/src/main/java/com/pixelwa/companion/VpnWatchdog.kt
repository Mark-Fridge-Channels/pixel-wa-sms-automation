package com.pixelwa.companion

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import android.util.Log
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Detects 青山 fake-online (UI connected, no tun / Google blocked) and
 * reconnects: stop → force-stop → start FAB → confirm VPN dialog.
 * Never connects on a node outside ALLOWED_REGIONS.
 */
object VpnWatchdog {
    private const val TAG = "WaVpnWatchdog"
    private val ALLOWED_REGIONS = listOf("美国", "加拿大")
    private const val BAD_NODE_TTL_MS = 30 * 60 * 1000L
    private const val MAX_LIST_SCROLLS = 8
    private val badNodes = ConcurrentHashMap<String, Long>()
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

            val node = ensureAllowedNode(rotate = false)
            if (node == null) {
                Log.e(TAG, "no US/CA node selected; refuse to connect")
                SendAccessibilityService.goHome()
                return false
            }
            SendAccessibilityService.dismissPermissionDialogs()
            val started = startIfIdle()
            Log.i(TAG, "connect card clicked=$started node=$node")
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
                Log.w(TAG, "still down on $node, try force-stop path with another US/CA node")
                badNodes[node] = System.currentTimeMillis()
                openAppInfo(context)
                sleep(2500)
                val stopped = SendAccessibilityService.forceStopCurrentApp()
                Log.i(TAG, "force stop clicked=$stopped")
                sleep(2000)
                openQingshan(context)
                waitFor { SendAccessibilityService.hasQingshanConnectCard() }
                SendAccessibilityService.dismissPermissionDialogs()
                val node2 = ensureAllowedNode(rotate = true)
                if (node2 == null) {
                    Log.e(TAG, "no US/CA node selected on retry; refuse to connect")
                    SendAccessibilityService.goHome()
                    return false
                }
                SendAccessibilityService.dismissPermissionDialogs()
                val started2 = startIfIdle()
                Log.i(TAG, "connect retry clicked=$started2 node=$node2")
                sleep(1200)
                SendAccessibilityService.confirmVpnPrepareDialog()
                val deadline2 = System.currentTimeMillis() + 20_000L
                while (System.currentTimeMillis() < deadline2) {
                    if (VpnHealth.hasVpnTransport(context)) break
                    sleep(1000)
                }
                VpnHealth.invalidate()
                snap = VpnHealth.probe(context, prefs, force = true)
                if (!snap.healthy) badNodes[node2] = System.currentTimeMillis()
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

    fun isAllowedNode(name: String?): Boolean =
        name != null && ALLOWED_REGIONS.any { name.contains(it) }

    private fun isBad(name: String): Boolean {
        val at = badNodes[name] ?: return false
        return System.currentTimeMillis() - at < BAD_NODE_TTL_MS
    }

    /**
     * Leaves 青山 on a US/CA node and returns its name, or null if that is impossible.
     * rotate=true moves off the current node to another allowed one when there is one.
     */
    private fun ensureAllowedNode(rotate: Boolean): String? {
        waitFor(3_000L) { SendAccessibilityService.qingshanCurrentServer() != null }
        val current = SendAccessibilityService.qingshanCurrentServer()
        Log.i(TAG, "current node=$current rotate=$rotate")
        if (!rotate && current != null && isAllowedNode(current) && !isBad(current)) return current

        val passes = listOf<(String, String) -> Boolean>(
            { name, status -> name != current && status == "正常" && !isBad(name) },
            { name, _ -> name != current && !isBad(name) },
            { name, _ -> name != current },
        )
        for (accept in passes) {
            val picked = selectFromList { name, status -> isAllowedNode(name) && accept(name, status) }
                ?: continue
            val now = SendAccessibilityService.qingshanCurrentServer()
            Log.i(TAG, "picked node=$picked shown=$now")
            if (isAllowedNode(now)) return now
        }
        val fallback = SendAccessibilityService.qingshanCurrentServer()
        return fallback?.takeIf { isAllowedNode(it) }
    }

    /** Opens 线路列表, clicks the first row accepted from the top; closes the list on miss. */
    private fun selectFromList(accept: (String, String) -> Boolean): String? {
        if (!SendAccessibilityService.openQingshanServerList()) return null
        if (!waitFor(5_000L) { SendAccessibilityService.qingshanServerRows() != null }) return null
        for (i in 0 until MAX_LIST_SCROLLS) {
            if (!SendAccessibilityService.scrollQingshanServers(forward = false)) break
            sleep(300)
        }
        var lastNames: List<String>? = null
        for (i in 0..MAX_LIST_SCROLLS) {
            val rows = SendAccessibilityService.qingshanServerRows() ?: break
            val hit = rows.firstOrNull { (name, status) -> accept(name, status) }?.first
            if (hit != null) {
                if (!SendAccessibilityService.clickQingshanServer(hit)) break
                waitFor { SendAccessibilityService.hasQingshanConnectCard() }
                return hit
            }
            val names = rows.map { it.first }
            if (names == lastNames || !SendAccessibilityService.scrollQingshanServers(forward = true)) break
            lastNames = names
            sleep(500)
        }
        SendAccessibilityService.closeQingshanServerList()
        waitFor(3_000L) { SendAccessibilityService.hasQingshanConnectCard() }
        return null
    }

    /** Selecting a node can auto-connect; tapping the card then would disconnect. */
    private fun startIfIdle(): Boolean {
        val label = SendAccessibilityService.qingshanConnectLabel().orEmpty()
        if (label.contains("已连接") || label.contains("连接中") ||
            label.contains("Connected", ignoreCase = true)
        ) {
            Log.i(TAG, "already $label; skip start tap")
            return true
        }
        return SendAccessibilityService.clickQingshanStart()
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
