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

    companion object {
        private const val KEY_URL = "server_url"
        private const val KEY_TOKEN = "api_token"
        private const val KEY_ON = "automation_on"
    }
}
