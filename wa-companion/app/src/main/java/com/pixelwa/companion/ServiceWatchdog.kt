package com.pixelwa.companion

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationCompat
import java.util.concurrent.atomic.AtomicLong

/**
 * Keeps polling alive and surfaces accessibility / FGS failures on a dedicated handset.
 */
object ServiceWatchdog {
    private const val TAG = "WaWatchdog"
    private const val ALERT_CHANNEL = "wa_companion_alerts"
    private const val ALERT_A11Y_ID = 1001
    private const val ALERT_POLL_ID = 1002
    private const val MIN_ENSURE_GAP_MS = 20_000L
    private const val LOCAL_ALERT_AFTER_MS = 15L * 60L * 1000L

    private val lastEnsureAt = AtomicLong(0L)
    @Volatile
    private var unhealthySinceMs: Long = 0L

    fun ensureAlive(context: Context, force: Boolean = false) {
        val prefs = Prefs(context)
        if (!prefs.automationEnabled) {
            unhealthySinceMs = 0L
            return
        }

        val now = System.currentTimeMillis()
        if (!force && now - lastEnsureAt.get() < MIN_ENSURE_GAP_MS) return
        lastEnsureAt.set(now)

        ensureAlertChannel(context)
        val a11yOk = checkAccessibility(context)
        val pollOk = checkPolling(context, prefs)
        if (a11yOk && pollOk) {
            unhealthySinceMs = 0L
        } else {
            if (unhealthySinceMs == 0L) unhealthySinceMs = now
            val age = now - unhealthySinceMs
            if (age >= LOCAL_ALERT_AFTER_MS) {
                val reasons = buildList {
                    if (!a11yOk) add("a11y_off")
                    if (!pollOk) add("poll_dead")
                }.joinToString(",")
                AlertSms.maybeSend(
                    context,
                    prefs,
                    "unrecovered ${age / 60000}m: $reasons (auto-restart failed)",
                )
            }
        }
    }

    /** Snapshot for orchestrator heartbeat / monitor. */
    fun healthExtras(context: Context, prefs: Prefs): Map<String, Any?> {
        val ver = try {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName
        } catch (_: Exception) {
            "?"
        }
        return mapOf(
            "poll_alive" to PollingForegroundService.isAliveRecently(),
            "a11y_bound" to SendAccessibilityService.isBound(),
            "a11y_settings" to AccessibilityRestorer.isEnabledInSettings(context),
            "automation_on" to prefs.automationEnabled,
            "app_version" to ver,
            "boot_elapsed_ms" to SystemClock.elapsedRealtime(),
        )
    }

    private fun checkPolling(context: Context, prefs: Prefs): Boolean {
        if (PollingForegroundService.isAliveRecently()) {
            cancel(context, ALERT_POLL_ID)
            return true
        }
        Log.w(TAG, "polling not alive recently; ensuring start")
        val started = PollingForegroundService.ensureStarted(context.applicationContext)
        if (PollingForegroundService.isAliveRecently(maxAgeMs = 90_000L)) {
            cancel(context, ALERT_POLL_ID)
            return true
        }
        if (!started) {
            notifyPollDown(context)
            tryBringAppToFront(context)
        } else {
            Log.i(TAG, "polling ensureStarted=$started waiting for first tick")
        }
        return PollingForegroundService.isAliveRecently(maxAgeMs = 90_000L)
    }

    private fun checkAccessibility(context: Context): Boolean {
        if (SendAccessibilityService.isBound()) {
            cancel(context, ALERT_A11Y_ID)
            return true
        }
        Log.w(TAG, "accessibility not bound")
        val restored = AccessibilityRestorer.tryEnable(context)
        // Binding can take a moment after settings write.
        if (restored) {
            try {
                Thread.sleep(800)
            } catch (_: InterruptedException) {
            }
        }
        if (SendAccessibilityService.isBound()) {
            Log.i(TAG, "accessibility restored via WRITE_SECURE_SETTINGS")
            cancel(context, ALERT_A11Y_ID)
            return true
        }
        notifyAccessibilityOff(context)
        return false
    }

    fun accessibilityStatusLine(context: Context): String {
        return when {
            SendAccessibilityService.isBound() -> "a11y OK"
            AccessibilityRestorer.isEnabledInSettings(context) -> "a11y in settings (binding…)"
            else -> "a11y OFF — re-enable required"
        }
    }

    private fun tryBringAppToFront(context: Context) {
        try {
            val launch = Intent(context, MainActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                putExtra(MainActivity.EXTRA_WATCHDOG_RESTART, true)
            }
            context.startActivity(launch)
        } catch (e: Exception) {
            Log.w(TAG, "bring app to front failed", e)
        }
    }

    private fun ensureAlertChannel(context: Context) {
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(
            NotificationChannel(
                ALERT_CHANNEL,
                "WA Companion alerts",
                NotificationManager.IMPORTANCE_HIGH,
            )
        )
    }

    private fun notifyAccessibilityOff(context: Context) {
        val open = PendingIntent.getActivity(
            context,
            1,
            Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val n = NotificationCompat.Builder(context, ALERT_CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_error)
            .setContentTitle("WA Companion: Accessibility OFF")
            .setContentText("Re-enable WA Companion accessibility (APK updates clear it).")
            .setContentIntent(open)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        context.getSystemService(NotificationManager::class.java)?.notify(ALERT_A11Y_ID, n)
    }

    private fun notifyPollDown(context: Context) {
        val open = PendingIntent.getActivity(
            context,
            2,
            Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val n = NotificationCompat.Builder(context, ALERT_CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_error)
            .setContentTitle("WA Companion: polling stopped")
            .setContentText("Open the app to restart job polling.")
            .setContentIntent(open)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        context.getSystemService(NotificationManager::class.java)?.notify(ALERT_POLL_ID, n)
    }

    private fun cancel(context: Context, id: Int) {
        context.getSystemService(NotificationManager::class.java)?.cancel(id)
    }
}

/**
 * Dedicated-device helper: re-enable our AccessibilityService after APK updates when
 * WRITE_SECURE_SETTINGS has been granted via adb.
 */
object AccessibilityRestorer {
    private const val TAG = "WaA11yRestore"

    fun componentFlattened(context: Context): String =
        ComponentName(context, SendAccessibilityService::class.java).flattenToString()

    fun tryEnable(context: Context): Boolean {
        val component = componentFlattened(context)
        return try {
            val cr = context.contentResolver
            val current = Settings.Secure.getString(cr, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
            val already = current?.split(':')?.any { it.equals(component, ignoreCase = true) } == true
            if (!already) {
                val next = when {
                    current.isNullOrBlank() -> component
                    else -> "$current:$component"
                }
                val ok = Settings.Secure.putString(cr, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, next)
                if (!ok) {
                    Log.w(TAG, "putString ENABLED_ACCESSIBILITY_SERVICES failed (need WRITE_SECURE_SETTINGS)")
                    return false
                }
            }
            Settings.Secure.putInt(cr, Settings.Secure.ACCESSIBILITY_ENABLED, 1)
            Log.i(TAG, "enabled accessibility service $component")
            true
        } catch (e: SecurityException) {
            Log.w(TAG, "no WRITE_SECURE_SETTINGS: ${e.message}")
            false
        } catch (e: Exception) {
            Log.e(TAG, "tryEnable failed", e)
            false
        }
    }

    fun isEnabledInSettings(context: Context): Boolean {
        val component = componentFlattened(context)
        val current = Settings.Secure.getString(
            context.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ) ?: return false
        return current.split(':').any { it.equals(component, ignoreCase = true) }
    }
}
