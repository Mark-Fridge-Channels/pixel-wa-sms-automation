package com.pixelwa.companion

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

data class WaJob(
    val id: String,
    val phone: String,
    val text: String,
    val taskId: String? = null,
    val mediaUrl: String? = null,
    val mediaType: String? = null,
) {
    val hasMedia: Boolean
        get() = !mediaUrl.isNullOrBlank() && mediaType in setOf("image", "video")
}

class OrchestratorApi(private val prefs: Prefs) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private fun authHeaders(builder: Request.Builder): Request.Builder {
        val token = prefs.apiToken
        if (token.isNotBlank()) {
            builder.header("Authorization", "Bearer $token")
        }
        return builder
    }

    fun heartbeat(vpn: VpnSnapshot? = null, extras: Map<String, Any?> = emptyMap()): Boolean {
        val json = JSONObject()
        if (vpn != null) {
            json.put("vpn_transport", vpn.vpnTransport)
                .put("google_ok", vpn.googleOk)
                .put("orch_ok", vpn.orchOk)
                .put("always_on_vpn", vpn.alwaysOn)
                .put("vpn_recovered", vpn.recovered)
                .put("vpn_detail", vpn.detail)
        }
        for ((k, v) in extras) {
            when (v) {
                null -> json.put(k, JSONObject.NULL)
                is Boolean -> json.put(k, v)
                is Number -> json.put(k, v)
                else -> json.put(k, v.toString())
            }
        }
        val body = json.toString().toRequestBody("application/json; charset=utf-8".toMediaType())
        val req = authHeaders(
            Request.Builder().url("${prefs.serverBaseUrl}/wa/heartbeat").post(body)
        ).build()
        client.newCall(req).execute().use { return it.isSuccessful }
    }

    fun nextJob(): WaJob? {
        val req = authHeaders(
            Request.Builder().url("${prefs.serverBaseUrl}/wa/jobs/next").get()
        ).build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) return null
            val body = resp.body?.string() ?: return null
            val json = JSONObject(body)
            val job = json.optJSONObject("job") ?: return null
            val id = job.optString("id")
            val phone = job.optString("phone")
            val text = job.optString("text")
            if (id.isBlank() || phone.isBlank()) return null
            val mediaUrl = job.optString("media_url").ifBlank { null }
            val mediaType = job.optString("media_type").ifBlank { null }
            return WaJob(
                id = id,
                phone = phone,
                text = text,
                taskId = job.optString("task_id").ifBlank { null },
                mediaUrl = mediaUrl,
                mediaType = mediaType,
            )
        }
    }

    fun reportResult(jobId: String, ok: Boolean, error: String? = null): Boolean {
        val payload = JSONObject()
            .put("ok", ok)
            .put("success", ok)
        if (error != null) payload.put("error", error)
        val media = "application/json; charset=utf-8".toMediaType()
        val req = authHeaders(
            Request.Builder()
                .url("${prefs.serverBaseUrl}/wa/jobs/$jobId/result")
                .post(payload.toString().toRequestBody(media))
        ).build()
        client.newCall(req).execute().use { return it.isSuccessful }
    }

    fun postInbound(from: String?, body: String?, receivedAt: Long?): Boolean {
        val payload = JSONObject()
            .put("from", from)
            .put("sender", from)
            .put("body", body)
            .put("text", body)
        if (receivedAt != null) {
            payload.put("received_at", java.time.Instant.ofEpochMilli(receivedAt).toString())
        }
        val media = "application/json; charset=utf-8".toMediaType()
        val req = authHeaders(
            Request.Builder()
                .url("${prefs.serverBaseUrl}/webhook/whatsapp")
                .post(payload.toString().toRequestBody(media))
        ).build()
        client.newCall(req).execute().use { return it.isSuccessful }
    }
}
