package com.pixelwa.companion

import android.content.Context
import android.os.PowerManager
import android.util.Log

/**
 * Wake the display so WhatsApp + Accessibility can run while the phone was asleep.
 * Dedicated outreach devices should use no PIN; this only turns the screen on.
 */
class ScreenWake(context: Context) {
    private val appContext = context.applicationContext
    private val lock = Any()
    private var wakeLock: PowerManager.WakeLock? = null

    @Suppress("DEPRECATION")
    fun acquire(timeoutMs: Long = 90_000L) {
        synchronized(lock) {
            releaseLocked()
            val pm = appContext.getSystemService(Context.POWER_SERVICE) as PowerManager
            val flags =
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK or
                    PowerManager.ACQUIRE_CAUSES_WAKEUP or
                    PowerManager.ON_AFTER_RELEASE
            val wl = pm.newWakeLock(flags, "WaCompanion:SendWake").apply {
                setReferenceCounted(false)
            }
            wl.acquire(timeoutMs)
            wakeLock = wl
            Log.i(TAG, "screen wake acquired timeoutMs=$timeoutMs")
        }
    }

    fun release() {
        synchronized(lock) {
            releaseLocked()
        }
    }

    private fun releaseLocked() {
        val wl = wakeLock ?: return
        wakeLock = null
        try {
            if (wl.isHeld) {
                wl.release()
                Log.i(TAG, "screen wake released")
            }
        } catch (e: RuntimeException) {
            Log.w(TAG, "screen wake release failed: ${e.message}")
        }
    }

    companion object {
        private const val TAG = "WaScreenWake"
    }
}
