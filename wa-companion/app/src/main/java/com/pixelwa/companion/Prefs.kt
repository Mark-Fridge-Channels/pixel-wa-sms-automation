package com.pixelwa.companion

import android.content.Context
import android.content.SharedPreferences

class Prefs(context: Context) {
    private val sp: SharedPreferences =
        context.getSharedPreferences("wa_companion", Context.MODE_PRIVATE)

    var serverBaseUrl: String
        get() = sp.getString(KEY_URL, "http://127.0.0.1:8787") ?: "http://127.0.0.1:8787"
        set(value) = sp.edit().putString(KEY_URL, value.trim().trimEnd('/')).apply()

    var apiToken: String
        get() = sp.getString(KEY_TOKEN, "") ?: ""
        set(value) = sp.edit().putString(KEY_TOKEN, value.trim()).apply()

    var automationEnabled: Boolean
        get() = sp.getBoolean(KEY_ON, false)
        set(value) = sp.edit().putBoolean(KEY_ON, value).apply()

    var vpnWatchdogEnabled: Boolean
        get() = sp.getBoolean(KEY_VPN_WATCHDOG, true)
        set(value) = sp.edit().putBoolean(KEY_VPN_WATCHDOG, value).apply()

    var lastVpnRecoverAtMs: Long
        get() = sp.getLong(KEY_VPN_LAST_RECOVER, 0L)
        set(value) = sp.edit().putLong(KEY_VPN_LAST_RECOVER, value).apply()

    var vpnRecoverBurst: Int
        get() = sp.getInt(KEY_VPN_BURST, 0)
        set(value) = sp.edit().putInt(KEY_VPN_BURST, value).apply()

    var vpnRecoverBurstWindowStartMs: Long
        get() = sp.getLong(KEY_VPN_BURST_WINDOW, 0L)
        set(value) = sp.edit().putLong(KEY_VPN_BURST_WINDOW, value).apply()

    companion object {
        private const val KEY_URL = "server_url"
        private const val KEY_TOKEN = "api_token"
        private const val KEY_ON = "automation_on"
        private const val KEY_VPN_WATCHDOG = "vpn_watchdog"
        private const val KEY_VPN_LAST_RECOVER = "vpn_last_recover_ms"
        private const val KEY_VPN_BURST = "vpn_recover_burst"
        private const val KEY_VPN_BURST_WINDOW = "vpn_recover_burst_window"
    }
}
