package com.pixelwa.companion

import android.content.Context
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import android.util.Log
import androidx.core.content.FileProvider
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.util.concurrent.TimeUnit

object MediaHelper {
    private const val TAG = "WaMedia"
    private val http = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(600, TimeUnit.SECONDS)
        .writeTimeout(600, TimeUnit.SECONDS)
        .build()

    fun mimeFor(mediaType: String, filename: String? = null): String {
        val lower = filename?.lowercase().orEmpty()
        return when {
            lower.endsWith(".png") -> "image/png"
            lower.endsWith(".webp") -> "image/webp"
            lower.endsWith(".gif") -> "image/gif"
            lower.endsWith(".mp4") -> "video/mp4"
            lower.endsWith(".3gp") -> "video/3gpp"
            lower.endsWith(".m4a") -> "audio/mp4"
            lower.endsWith(".ogg") -> "audio/ogg"
            lower.endsWith(".opus") -> "audio/ogg"
            lower.endsWith(".mp3") -> "audio/mpeg"
            mediaType == "image" -> "image/jpeg"
            mediaType == "video" -> "video/mp4"
            mediaType == "audio" -> "audio/mpeg"
            else -> "application/octet-stream"
        }
    }

    fun downloadToCache(context: Context, url: String, mediaType: String): File? {
        return try {
            val ext = when (mediaType) {
                "image" -> ".jpg"
                "video" -> ".mp4"
                "audio" -> ".m4a"
                else -> ".bin"
            }
            val nameFromUrl = url.substringAfterLast('/').substringBefore('?')
            val safe = nameFromUrl.ifBlank { "media$ext" }.replace(Regex("[^A-Za-z0-9._-]"), "_")
            // Reuse a completed prior download of the same basename (skip 100MB+ re-fetch).
            val existing = context.cacheDir.listFiles()
                ?.filter { it.isFile && it.name.endsWith("_$safe") && it.length() > 1024 }
                ?.maxByOrNull { it.lastModified() }
            if (existing != null) {
                Log.i(TAG, "reuse cached media ${existing.name} size=${existing.length()}")
                return existing
            }
            val req = Request.Builder().url(url).get().build()
            http.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) {
                    Log.e(TAG, "download failed code=${resp.code}")
                    return null
                }
                val out = File(context.cacheDir, "out_${System.currentTimeMillis()}_$safe")
                resp.body?.byteStream()?.use { input ->
                    out.outputStream().use { output -> input.copyTo(output) }
                } ?: return null
                out
            }
        } catch (e: Exception) {
            Log.e(TAG, "download error", e)
            null
        }
    }

    fun fileProviderUri(context: Context, file: File): Uri {
        return FileProvider.getUriForFile(
            context,
            "${context.packageName}.fileprovider",
            file,
        )
    }

    /** Best-effort: newest inbound file under WhatsApp Media dirs matching type. */
    fun findRecentWhatsAppMedia(mediaType: String, maxAgeMs: Long = 60_000L): File? {
        val roots = listOf(
            File(Environment.getExternalStorageDirectory(), "Android/media/com.whatsapp/WhatsApp/Media"),
            File(Environment.getExternalStorageDirectory(), "WhatsApp/Media"),
        )
        val sub = when (mediaType) {
            "image" -> listOf("WhatsApp Images", "WhatsApp Image")
            "video" -> listOf("WhatsApp Video", "WhatsApp Video Notes")
            "audio" -> listOf("WhatsApp Audio", "WhatsApp Voice Notes")
            else -> listOf(
                "WhatsApp Documents",
                "WhatsApp Images",
                "WhatsApp Video",
                "WhatsApp Audio",
                "WhatsApp Voice Notes",
            )
        }
        val cutoff = System.currentTimeMillis() - maxAgeMs
        var best: File? = null
        var bestMtime = 0L
        for (root in roots) {
            if (!root.exists()) continue
            for (name in sub) {
                val dir = File(root, name)
                if (!dir.isDirectory) continue
                // Voice Notes use year/week subdirs; Private/Sent also nested.
                dir.walkTopDown().maxDepth(5).forEach { f ->
                    if (!f.isFile) return@forEach
                    if (f.name == ".nomedia") return@forEach
                    if (f.name.startsWith("notif_preview_")) return@forEach
                    // Inbound only — skip our own outbound copies under Sent/.
                    val path = f.absolutePath
                    if (path.contains("/Sent/") || path.contains("/Private/Sent/")) return@forEach
                    if (!isPlausibleInboundMedia(f, mediaType)) return@forEach
                    val m = f.lastModified()
                    if (m >= cutoff && m > bestMtime) {
                        best = f
                        bestMtime = m
                    }
                }
            }
        }
        return best
    }

    /**
     * Reject tiny placeholders / avatar-sized junk that must never be uploaded as inbound media.
     */
    fun isPlausibleInboundMedia(file: File, mediaType: String): Boolean {
        if (!file.isFile) return false
        val size = file.length()
        val min = when (mediaType) {
            "image" -> 8_192L // real photos/thumbs on disk; avatars/notif previews are often << this
            "video" -> 32_768L
            "audio" -> 2_048L
            else -> 256L
        }
        return size >= min
    }

    /** Fallback: newest MediaStore item (when WA writes through gallery). */
    fun findRecentMediaStore(context: Context, mediaType: String, maxAgeMs: Long = 120_000L): File? {
        val uri: Uri
        val collection: String
        when (mediaType) {
            "image" -> {
                uri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI
                collection = MediaStore.Images.Media.DATA
            }
            "video" -> {
                uri = MediaStore.Video.Media.EXTERNAL_CONTENT_URI
                collection = MediaStore.Video.Media.DATA
            }
            "audio" -> {
                uri = MediaStore.Audio.Media.EXTERNAL_CONTENT_URI
                collection = MediaStore.Audio.Media.DATA
            }
            else -> {
                uri = MediaStore.Files.getContentUri("external")
                collection = MediaStore.Files.FileColumns.DATA
            }
        }
        val cutoffSec = (System.currentTimeMillis() - maxAgeMs) / 1000L
        return try {
            context.contentResolver.query(
                uri,
                arrayOf(collection, MediaStore.MediaColumns.DATE_ADDED),
                "${MediaStore.MediaColumns.DATE_ADDED}>=?",
                arrayOf(cutoffSec.toString()),
                "${MediaStore.MediaColumns.DATE_ADDED} DESC",
            )?.use { c ->
                val dataIdx = c.getColumnIndex(collection)
                if (dataIdx < 0) return null
                while (c.moveToNext()) {
                    val path = c.getString(dataIdx) ?: continue
                    if (path.contains("/Sent/") || path.contains("/Private/Sent/")) continue
                    val f = File(path)
                    if (isPlausibleInboundMedia(f, mediaType)) return f
                }
                null
            }
        } catch (e: Exception) {
            Log.w(TAG, "MediaStore query failed", e)
            null
        }
    }

    fun detectMediaTypeFromNotification(text: String): String? {
        val t = text.lowercase()
        return when {
            "video" in t || "视频" in t || "📹" in text || "🎥" in text ||
                "gif" in t -> "video"
            "photo" in t || "image" in t || "图片" in t || "📷" in text || "🖼️" in text ||
                "sticker" in t || "贴纸" in t -> "image"
            "audio" in t || "voice" in t || "语音" in t || "ptt" in t || "🎵" in text ||
                "🎤" in text || "voice message" in t || "语音消息" in t ||
                "audio message" in t || "录音" in t -> "audio"
            "document" in t || "file" in t || "文档" in t || "📎" in text ||
                "pdf" in t || "contact card" in t || "联系人卡片" in t -> "file"
            // Common WA template strings (EN / ZH)
            "shared a photo" in t || "shared an image" in t || "发来一张图片" in t ||
                "发来一张照片" in t -> "image"
            "shared a video" in t || "发来一段视频" in t -> "video"
            "shared a voice" in t || "shared an audio" in t || "发来一条语音" in t -> "audio"
            else -> null
        }
    }

    fun uploadInboundMedia(
        prefs: Prefs,
        file: File,
        from: String?,
        caption: String?,
        mediaType: String,
        receivedAt: Long?,
    ): Boolean {
        val media = mimeFor(mediaType, file.name).toMediaType()
        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("from", from.orEmpty())
            .addFormDataPart("sender", from.orEmpty())
            .addFormDataPart("body", caption.orEmpty())
            .addFormDataPart("media_type", mediaType)
            .addFormDataPart(
                "file",
                file.name,
                file.asRequestBody(media),
            )
        if (receivedAt != null) {
            body.addFormDataPart(
                "received_at",
                java.time.Instant.ofEpochMilli(receivedAt).toString(),
            )
        }
        val reqBuilder = Request.Builder()
            .url("${prefs.serverBaseUrl}/webhook/whatsapp/media")
            .post(body.build())
        val token = prefs.apiToken
        if (token.isNotBlank()) {
            reqBuilder.header("Authorization", "Bearer $token")
        }
        return try {
            http.newCall(reqBuilder.build()).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            Log.e(TAG, "media upload failed", e)
            false
        }
    }
}
