package com.pixelwa.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Start polling after boot or after APK update (updates clear accessibility unless restored).
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val action = intent?.action ?: return
        if (action != Intent.ACTION_BOOT_COMPLETED &&
            action != Intent.ACTION_MY_PACKAGE_REPLACED &&
            action != Intent.ACTION_LOCKED_BOOT_COMPLETED
        ) {
            return
        }
        Log.i(TAG, "boot/replace action=$action")
        val prefs = Prefs(context)
        AccessibilityRestorer.tryEnable(context)
        if (!prefs.automationEnabled) return
        PollingForegroundService.ensureStarted(context)
        ServiceWatchdog.ensureAlive(context, force = true)
    }

    companion object {
        private const val TAG = "WaBoot"
    }
}
