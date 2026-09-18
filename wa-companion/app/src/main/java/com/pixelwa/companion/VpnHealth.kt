package com.pixelwa.companion

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.provider.Settings
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

data class VpnSnapshot(
    val vpnTransport: Boolean,
    val googleOk: Boolean,
    val orchOk: Boolean,
    val alwaysOn: Boolean,
    val recovered: Boolean = false,
    val detail: String,
) {
    /** WhatsApp needs unblocked HTTPS (Google/WA), not just orch. */
    val healthy: Boolean get() = googleOk
}

object VpnHealth {
    private const val TAG = "WaVpnHealth"
    const val QS_PACKAGE = "com.wieifjyr.qs2"
    private const val GOOGLE_204 = "https://www.google.com/generate_204"

    private val probeClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .followRedirects(false)
        .followSslRedirects(false)
        .build()

    @Volatile
    private var cachedAt = 0L

    @Volatile
    private var cached: VpnSnapshot? = null

    fun hasVpnTransport(context: Context): Boolean {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val networks = cm.allNetworks
        for (n in networks) {
            val caps = cm.getNetworkCapabilities(n) ?: continue
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) return true
        }
        return false
    }

    fun alwaysOnQingshan(context: Context): Boolean {
        return try {
            val pkg = Settings.Secure.getString(context.contentResolver, "always_on_vpn_app")
            pkg == QS_PACKAGE
        } catch (_: Exception) {
            false
        }
    }

    fun probe(context: Context, prefs: Prefs, force: Boolean = false): VpnSnapshot {
        val now = System.currentTimeMillis()
        val hit = cached
        if (!force && hit != null && hit.healthy && now - cachedAt < 20_000L) {
            return hit
        }
        val vpn = hasVpnTransport(context)
        val google = httpOk(GOOGLE_204, expectNoContent = true)
        val orch = httpOk("${prefs.serverBaseUrl.trimEnd('/')}/health", expectNoContent = false)
        val alwaysOn = alwaysOnQingshan(context)
        val detail = buildString {
            append("vpn=").append(vpn)
            append(" google=").append(google)
            append(" orch=").append(orch)
            append(" alwaysOn=").append(alwaysOn)
        }
        val snap = VpnSnapshot(
            vpnTransport = vpn,
            googleOk = google,
            orchOk = orch,
            alwaysOn = alwaysOn,
            detail = detail,
        )
        Log.i(TAG, detail)
        cached = snap
        cachedAt = now
        return snap
    }

    fun invalidate() {
        cached = null
        cachedAt = 0L
    }

    private fun httpOk(url: String, expectNoContent: Boolean): Boolean {
        return try {
            val req = Request.Builder().url(url).get().build()
            probeClient.newCall(req).execute().use { resp ->
                if (expectNoContent) {
                    resp.code == 204 || resp.code in 200..399
                } else {
                    resp.isSuccessful
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "probe fail $url: ${e.message}")
            false
        }
    }
}
