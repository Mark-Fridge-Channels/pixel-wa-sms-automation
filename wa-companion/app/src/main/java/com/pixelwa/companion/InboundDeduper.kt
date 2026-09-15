package com.pixelwa.companion

import java.util.concurrent.ConcurrentHashMap

/**
 * Short-window dedupe so notification + chat scrape don't double-post the same reply.
 */
object InboundDeduper {
    private const val WINDOW_MS = 120_000L
    private val seen = ConcurrentHashMap<String, Long>()

    fun shouldReport(from: String?, body: String): Boolean {
        val key = "${from.orEmpty().trim()}|${body.trim()}"
        if (key.length <= 1) return false
        val now = System.currentTimeMillis()
        prune(now)
        val prev = seen.putIfAbsent(key, now)
        return prev == null
    }

    private fun prune(now: Long) {
        val it = seen.entries.iterator()
        while (it.hasNext()) {
            val e = it.next()
            if (now - e.value > WINDOW_MS) it.remove()
        }
    }
}
