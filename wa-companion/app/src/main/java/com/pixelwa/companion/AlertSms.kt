package com.pixelwa.companion

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.telephony.SmsManager
import android.util.Log
import androidx.core.content.ContextCompat
import java.util.concurrent.atomic.AtomicLong

/**
 * Last-resort ops SMS when Companion cannot recover. Uses SmsManager (cellular),
 * independent of VPN / orchestrator reachability.
 */
object AlertSms {
    private const val TAG = "WaAlertSms"
    private val lastSentAt = AtomicLong(0L)
    private const val COOLDOWN_MS = 60L * 60L * 1000L

    fun maybeSend(context: Context, prefs: Prefs, reason: String): Boolean {
        if (!prefs.alertSmsEnabled) return false
        val to = prefs.alertSmsTo.filter { it.isDigit() || it == '+' }
        if (to.length < 8) return false
        val now = System.currentTimeMillis()
        if (now - lastSentAt.get() < COOLDOWN_MS) {
            Log.i(TAG, "skip SMS cooldown reason=$reason")
            return false
        }
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.SEND_SMS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            Log.w(TAG, "SEND_SMS not granted")
            return false
        }
        val body = "[WA Companion] $reason".take(300)
        return try {
            val sms = if (Build.VERSION.SDK_INT >= 31) {
                context.getSystemService(SmsManager::class.java)
            } else {
                @Suppress("DEPRECATION")
                SmsManager.getDefault()
            }
            if (sms == null) {
                Log.e(TAG, "SmsManager null")
                return false
            }
            val parts = sms.divideMessage(body)
            if (parts.size == 1) {
                sms.sendTextMessage(to, null, body, null, null)
            } else {
                sms.sendMultipartTextMessage(to, null, parts, null, null)
            }
            lastSentAt.set(now)
            prefs.lastAlertSmsAtMs = now
            Log.w(TAG, "alert SMS sent to=$to reason=$reason")
            true
        } catch (e: Exception) {
            Log.e(TAG, "send failed", e)
            false
        }
    }
}
