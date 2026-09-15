package com.pixelwa.companion

import android.accessibilityservice.AccessibilityService
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.concurrent.atomic.AtomicBoolean

/**
 * After WhatsApp open Intent, fill composer if needed and click send.
 * Also exposes chat message snapshot for post-send inbound scrape.
 */
class SendAccessibilityService : AccessibilityService() {
    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (!armed.get()) return
        if (event?.packageName?.toString() != "com.whatsapp") return
        val root = rootInActiveWindow ?: return

        val notRegistered = detectNotOnWhatsApp(root)
        if (notRegistered != null) {
            Log.w(TAG, "number not on WhatsApp: $notRegistered")
            armed.set(false)
            SendCoordinator.onSendAttempted(false, notRegistered)
            return
        }

        dismissGateDialogs(root)

        val want = SendCoordinator.pendingText
        if (!want.isNullOrBlank()) {
            ensureComposerText(root, want)
        }

        if (clickSend(root)) {
            Log.i(TAG, "send button clicked")
            armed.set(false)
            SendCoordinator.onSendAttempted(true, null)
        }
    }

    override fun onInterrupt() {}

    private fun dismissGateDialogs(root: AccessibilityNodeInfo) {
        // New-number / first-chat confirmations
        for (label in listOf("Continue to Chat", "Continue to chat", "OK")) {
            val nodes = root.findAccessibilityNodeInfosByText(label) ?: continue
            for (n in nodes) {
                if (n.isClickable) {
                    n.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                    return
                }
                n.parent?.takeIf { it.isClickable }?.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            }
        }
    }

    /**
     * WhatsApp shows a dialog / screen when the number has no account.
     * Return a Chinese failure reason for Notion Notes, or null if not detected.
     */
    private fun detectNotOnWhatsApp(root: AccessibilityNodeInfo): String? {
        val needles = listOf(
            "isn't on WhatsApp",
            "isn't on whatsapp",
            "not on WhatsApp",
            "not on whatsapp",
            "Phone number shared via url is invalid",
            "phone number shared via url is invalid",
            "不在 WhatsApp",
            "未使用 WhatsApp",
            "没有 WhatsApp",
            "无法使用 WhatsApp",
        )
        val blob = StringBuilder()
        collectText(root, blob, 0)
        val hay = blob.toString()
        for (n in needles) {
            if (hay.contains(n, ignoreCase = true)) {
                return "号码未注册 WhatsApp（界面提示：$n）"
            }
        }
        return null
    }

    private fun collectText(node: AccessibilityNodeInfo, out: StringBuilder, depth: Int) {
        if (depth > 12) return
        node.text?.toString()?.takeIf { it.isNotBlank() }?.let { out.append(it).append('\n') }
        node.contentDescription?.toString()?.takeIf { it.isNotBlank() }?.let {
            out.append(it).append('\n')
        }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            collectText(child, out, depth + 1)
            child.recycle()
        }
    }

    private fun ensureComposerText(root: AccessibilityNodeInfo, text: String): Boolean {
        val entries = root.findAccessibilityNodeInfosByViewId("com.whatsapp:id/entry")
        if (entries.isNullOrEmpty()) return false
        val entry = entries[0]
        val current = entry.text?.toString().orEmpty()
        // Hint often reads "Message" when empty
        if (current == text) return true
        if (current.isNotBlank() && current != "Message" && current.contains(text.take(20))) {
            return true
        }
        entry.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        val ok = entry.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        Log.i(TAG, "set composer text ok=$ok len=${text.length}")
        return ok
    }

    private fun clickSend(root: AccessibilityNodeInfo): Boolean {
        val candidates = listOf(
            "com.whatsapp:id/send",
            "com.whatsapp:id/conversation_entry_action_button",
            "com.whatsapp:id/send_button",
        )
        for (id in candidates) {
            val nodes = root.findAccessibilityNodeInfosByViewId(id)
            if (!nodes.isNullOrEmpty()) {
                val n = nodes[0]
                if (n.isClickable) {
                    n.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                    return true
                }
                n.parent?.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                return true
            }
        }
        // content-desc "Send" — avoid matching unrelated nodes
        val byDesc = root.findAccessibilityNodeInfosByText("Send")
        if (!byDesc.isNullOrEmpty()) {
            for (n in byDesc) {
                val desc = n.contentDescription?.toString().orEmpty()
                val t = n.text?.toString().orEmpty()
                if (desc.equals("Send", ignoreCase = true) || t.equals("Send", ignoreCase = true)) {
                    if (n.isClickable) {
                        n.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                        return true
                    }
                    n.parent?.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                    return true
                }
            }
        }
        return false
    }

    private fun collectMessageTexts(): Set<String> {
        val root = rootInActiveWindow ?: return emptySet()
        if (root.packageName?.toString() != "com.whatsapp") return emptySet()
        val out = linkedSetOf<String>()

        val byId = root.findAccessibilityNodeInfosByViewId("com.whatsapp:id/message_text")
        if (!byId.isNullOrEmpty()) {
            for (n in byId) {
                val t = n.text?.toString()?.trim().orEmpty()
                if (t.isNotBlank() && !isChromeText(t)) out.add(t)
            }
        }

        if (out.isEmpty()) {
            walk(root, out)
        }
        return out
    }

    private fun walk(node: AccessibilityNodeInfo, out: MutableSet<String>) {
        val t = node.text?.toString()?.trim().orEmpty()
        if (t.isNotBlank() && !isChromeText(t) && looksLikeMessage(t, node)) {
            out.add(t)
        }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            walk(child, out)
        }
    }

    private fun looksLikeMessage(text: String, node: AccessibilityNodeInfo): Boolean {
        if (TIME_RE.matches(text)) return false
        if (text.length < 1) return false
        val desc = node.contentDescription?.toString().orEmpty()
        if (desc.equals("Message", ignoreCase = true)) return false
        val cls = node.className?.toString().orEmpty()
        return cls.contains("TextView", ignoreCase = true) ||
            node.viewIdResourceName?.contains("message", ignoreCase = true) == true
    }

    companion object {
        private const val TAG = "WaSendA11y"
        private val armed = AtomicBoolean(false)
        private val TIME_RE = Regex("""^\d{1,2}:\d{2}(\s?[AP]M)?$""", RegexOption.IGNORE_CASE)

        @Volatile
        private var instance: SendAccessibilityService? = null

        fun armForSend() {
            armed.set(true)
            instance?.scheduleSendTicks()
        }

        fun disarm() {
            armed.set(false)
            instance?.cancelSendTicks()
        }

        fun snapshotMessages(): Set<String> {
            return try {
                instance?.collectMessageTexts() ?: emptySet()
            } catch (e: Exception) {
                Log.w(TAG, "snapshot failed", e)
                emptySet()
            }
        }

        private fun isChromeText(t: String): Boolean {
            val s = t.trim()
            if (s.equals("Message", ignoreCase = true)) return true
            if (s.equals("online", ignoreCase = true)) return true
            if (s.equals("Today", ignoreCase = true)) return true
            if (s.equals("Yesterday", ignoreCase = true)) return true
            if (s.equals("Type a message", ignoreCase = true)) return true
            if (s.contains("end-to-end encrypted", ignoreCase = true)) return true
            if (s.contains("Messages and calls", ignoreCase = true)) return true
            if (TIME_RE.matches(s)) return true
            if (Regex("""^\+\d[\d\s\-]+$""").matches(s)) return true
            return false
        }
    }

    private val tickHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val sendTick = object : Runnable {
        override fun run() {
            if (!armed.get()) return
            try {
                val root = rootInActiveWindow
                if (root != null && root.packageName?.toString() == "com.whatsapp") {
                    val notRegistered = detectNotOnWhatsApp(root)
                    if (notRegistered != null) {
                        Log.w(TAG, "number not on WhatsApp (tick): $notRegistered")
                        armed.set(false)
                        SendCoordinator.onSendAttempted(false, notRegistered)
                        return
                    }
                    dismissGateDialogs(root)
                    val want = SendCoordinator.pendingText
                    if (!want.isNullOrBlank()) ensureComposerText(root, want)
                    if (clickSend(root)) {
                        Log.i(TAG, "send button clicked (tick)")
                        armed.set(false)
                        SendCoordinator.onSendAttempted(true, null)
                        return
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "send tick failed", e)
            }
            tickHandler.postDelayed(this, 800L)
        }
    }

    private fun scheduleSendTicks() {
        cancelSendTicks()
        tickHandler.postDelayed(sendTick, 500L)
    }

    private fun cancelSendTicks() {
        tickHandler.removeCallbacks(sendTick)
    }
}

object SendCoordinator {
    @Volatile
    var pendingJobId: String? = null

    @Volatile
    var pendingText: String? = null

    @Volatile
    var callback: ((Boolean, String?) -> Unit)? = null

    fun onSendAttempted(ok: Boolean, error: String?) {
        val cb = callback
        callback = null
        pendingJobId = null
        pendingText = null
        SendAccessibilityService.disarm()
        cb?.invoke(ok, error)
    }
}
