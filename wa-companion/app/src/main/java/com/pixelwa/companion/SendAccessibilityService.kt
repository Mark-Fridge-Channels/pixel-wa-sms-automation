package com.pixelwa.companion

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.os.Looper
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
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
        attemptSend(root)
    }

    override fun onInterrupt() {}

    private fun dismissGateDialogs(root: AccessibilityNodeInfo) {
        // Share-confirm dialog: "Share with +86 …?" → OK
        val button1 = root.findAccessibilityNodeInfosByViewId("android:id/button1")
        if (!button1.isNullOrEmpty()) {
            val msg = root.findAccessibilityNodeInfosByViewId("android:id/message")
                ?.firstOrNull()?.text?.toString().orEmpty()
            if (msg.contains("Share with", ignoreCase = true) ||
                msg.contains("分享给", ignoreCase = true)
            ) {
                val n = button1[0]
                if (n.isClickable && n.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    Log.i(TAG, "clicked Share-with OK")
                    return
                }
                n.parent?.takeIf { it.isClickable }?.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                Log.i(TAG, "clicked Share-with OK via parent")
                return
            }
        }
        // New-number / first-chat confirmations
        for (label in listOf("Continue to Chat", "Continue to chat", "OK", "确定")) {
            val nodes = root.findAccessibilityNodeInfosByText(label) ?: continue
            for (n in nodes) {
                if (n.isClickable) {
                    n.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                    Log.i(TAG, "dismissed gate: $label")
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

    /**
     * Media share UI: only touch caption fields. Never write chat [entry] — that caused
     * the same caption to be sent as many standalone text bubbles while send was retried.
     */
    private fun attemptSend(root: AccessibilityNodeInfo) {
        val want = SendCoordinator.pendingText
        val mediaMode = SendCoordinator.pendingMedia
        val inMediaComposer = isMediaComposerOpen(root)

        if (!want.isNullOrBlank()) {
            if (mediaMode || inMediaComposer) {
                ensureCaptionText(root, want)
            } else {
                ensureComposerText(root, want)
            }
        }

        if (mediaMode && inMediaComposer) {
            val now = System.currentTimeMillis()
            if (now - lastMediaSendClickAt < MEDIA_SEND_DEBOUNCE_MS) {
                return
            }
        }

        if (!clickSend(root)) return

        if (isMediaComposerOpen(root)) {
            lastMediaSendClickAt = System.currentTimeMillis()
            Log.i(TAG, "send clicked but media composer still open; wait")
            return
        }
        Log.i(TAG, "send button clicked")
        armed.set(false)
        SendCoordinator.onSendAttempted(true, null)
    }

    private fun isMediaComposerOpen(root: AccessibilityNodeInfo): Boolean {
        return !root.findAccessibilityNodeInfosByViewId("com.whatsapp:id/caption_input")
            .isNullOrEmpty() ||
            !root.findAccessibilityNodeInfosByViewId("com.whatsapp:id/caption")
                .isNullOrEmpty()
    }

    private fun ensureCaptionText(root: AccessibilityNodeInfo, text: String): Boolean {
        val ids = listOf(
            "com.whatsapp:id/caption",
            "com.whatsapp:id/caption_input",
        )
        for (id in ids) {
            val nodes = root.findAccessibilityNodeInfosByViewId(id) ?: continue
            if (nodes.isEmpty()) continue
            val entry = nodes[0]
            val current = entry.text?.toString().orEmpty()
            if (current == text) return true
            if (current.isNotBlank() && current.contains(text.take(20))) return true
            entry.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
            val args = Bundle().apply {
                putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
            }
            val ok = entry.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
            Log.i(TAG, "set caption text id=$id ok=$ok len=${text.length}")
            return ok
        }
        return false
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
                if (!n.isEnabled) {
                    Log.i(TAG, "send node $id not enabled yet")
                    continue
                }
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
        private const val MEDIA_SEND_DEBOUNCE_MS = 2000L
        private val armed = AtomicBoolean(false)
        private val TIME_RE = Regex("""^\d{1,2}:\d{2}(\s?[AP]M)?$""", RegexOption.IGNORE_CASE)
        @Volatile
        private var lastMediaSendClickAt: Long = 0L

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

        fun isBound(): Boolean = instance != null

        /** True while an outbound send is armed or waiting on a11y click. */
        fun isSending(): Boolean =
            armed.get() || SendCoordinator.pendingJobId != null || SendCoordinator.callback != null

        fun goHome() {
            val svc = instance ?: return
            svc.tickHandler.post {
                try {
                    svc.performGlobalAction(GLOBAL_ACTION_HOME)
                } catch (e: Exception) {
                    Log.w(TAG, "HOME failed", e)
                }
            }
        }

        fun qingshanConnectLabel(): String? {
            return onMain(1200) { svc ->
                val root = svc.rootInActiveWindow ?: return@onMain false
                if (root.packageName?.toString() != "com.wieifjyr.qs2") return@onMain false
                val nodes = root.findAccessibilityNodeInfosByViewId("com.wieifjyr.qs2:id/tv_connect_status")
                val label = nodes?.firstOrNull()?.text?.toString()?.trim().orEmpty()
                lastQingshanLabel = label
                label.isNotEmpty()
            }.let { if (it) lastQingshanLabel else null }
        }

        @Volatile
        private var lastQingshanLabel: String? = null

        fun hasQingshanConnectCard(): Boolean {
            return onMain(1200) { svc ->
                val root = svc.rootInActiveWindow ?: return@onMain false
                if (root.packageName?.toString() != "com.wieifjyr.qs2") return@onMain false
                !root.findAccessibilityNodeInfosByViewId("com.wieifjyr.qs2:id/btn_connect_card").isNullOrEmpty()
            }
        }

        fun clickQingshanStopIfConnected(): Boolean {
            val label = qingshanConnectLabel().orEmpty()
            Log.i(TAG, "qingshan status=$label")
            if (label == "连接" || label.equals("Connect", ignoreCase = true)) {
                return false
            }
            if (label.contains("已连接") || label.contains("断开") ||
                label.contains("连接中") || label.contains("Connected", ignoreCase = true)
            ) {
                return clickViewId("com.wieifjyr.qs2:id/btn_connect_card")
            }
            return clickViewId("com.wieifjyr.qs2:id/fab", requireDescContains = "Stop") ||
                clickDescOrText("com.wieifjyr.qs2", "Stop service")
        }

        fun clickQingshanStart(): Boolean {
            dismissPermissionDialogs()
            val label = qingshanConnectLabel().orEmpty()
            Log.i(TAG, "qingshan start status=$label")
            if (clickViewId("com.wieifjyr.qs2:id/btn_connect_card")) return true
            if (label == "连接" && clickDescOrText("com.wieifjyr.qs2", "连接")) return true
            return clickViewId("com.wieifjyr.qs2:id/fab", requireDescContains = "Start") ||
                clickDescOrText("com.wieifjyr.qs2", "Start service")
        }

        fun dismissPermissionDialogs(): Boolean {
            return clickDescOrText(
                null,
                "Allow",
                "ALLOW",
                "允许",
                "始终允许",
            )
        }

        fun confirmVpnPrepareDialog(): Boolean {
            return clickDescOrText(
                "com.android.vpndialogs",
                "OK",
                "Allow",
                "确定",
                "允许",
            ) || clickDescOrText(null, "OK", "Allow", "确定")
        }

        fun forceStopCurrentApp(): Boolean {
            val clicked = clickDescOrText(
                "com.android.settings",
                "Force stop",
                "FORCE STOP",
                "强制停止",
            )
            if (!clicked) return false
            try {
                Thread.sleep(400)
            } catch (_: InterruptedException) {
                // ignore
            }
            clickDescOrText(null, "OK", "Force stop", "FORCE STOP", "确定", "强制停止")
            return true
        }

        private fun clickViewId(viewId: String, requireDescContains: String? = null): Boolean {
            return onMain(1200) { svc ->
                val root = svc.rootInActiveWindow ?: return@onMain false
                val nodes = root.findAccessibilityNodeInfosByViewId(viewId) ?: return@onMain false
                for (n in nodes) {
                    val desc = n.contentDescription?.toString().orEmpty()
                    if (requireDescContains != null &&
                        !desc.contains(requireDescContains, ignoreCase = true)
                    ) {
                        continue
                    }
                    if (clickNode(svc, n)) return@onMain true
                }
                false
            }
        }

        private fun clickDescOrText(packageName: String?, vararg labels: String): Boolean {
            return onMain(1200) { svc ->
                val root = svc.rootInActiveWindow ?: return@onMain false
                val pkg = root.packageName?.toString()
                if (packageName != null && pkg != packageName) return@onMain false
                for (label in labels) {
                    val nodes = root.findAccessibilityNodeInfosByText(label) ?: continue
                    for (n in nodes) {
                        val t = n.text?.toString().orEmpty()
                        val d = n.contentDescription?.toString().orEmpty()
                        if (!t.equals(label, ignoreCase = true) &&
                            !d.equals(label, ignoreCase = true) &&
                            !t.contains(label, ignoreCase = true) &&
                            !d.contains(label, ignoreCase = true)
                        ) {
                            continue
                        }
                        if (clickNode(svc, n)) return@onMain true
                    }
                }
                false
            }
        }

        private fun clickNode(svc: SendAccessibilityService, n: AccessibilityNodeInfo): Boolean {
            if (n.isClickable && n.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
            var p = n.parent
            var depth = 0
            while (p != null && depth < 8) {
                if (p.isClickable && p.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    return true
                }
                p = p.parent
                depth++
            }
            return tapNode(svc, n)
        }

        private fun tapNode(svc: SendAccessibilityService, n: AccessibilityNodeInfo): Boolean {
            val r = Rect()
            n.getBoundsInScreen(r)
            if (r.width() < 2 || r.height() < 2) return false
            val path = Path()
            path.moveTo(r.exactCenterX(), r.exactCenterY())
            val gesture = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(path, 0, 80))
                .build()
            return svc.dispatchGesture(gesture, null, null)
        }

        private fun onMain(timeoutMs: Long, block: (SendAccessibilityService) -> Boolean): Boolean {
            val svc = instance ?: return false
            if (Looper.myLooper() == Looper.getMainLooper()) {
                return try {
                    block(svc)
                } catch (e: Exception) {
                    Log.w(TAG, "a11y action failed", e)
                    false
                }
            }
            val latch = CountDownLatch(1)
            var result = false
            svc.tickHandler.post {
                result = try {
                    block(svc)
                } catch (e: Exception) {
                    Log.w(TAG, "a11y action failed", e)
                    false
                }
                latch.countDown()
            }
            return try {
                latch.await(timeoutMs, TimeUnit.MILLISECONDS)
                result
            } catch (_: InterruptedException) {
                false
            }
        }

        /** Leave open chat so WhatsApp is not stuck on the conversation screen. */
        fun leaveChatToList() {
            val svc = instance ?: return
            svc.tickHandler.post {
                try {
                    Log.i(TAG, "leaveChat: BACK")
                    svc.performGlobalAction(GLOBAL_ACTION_BACK)
                } catch (e: Exception) {
                    Log.w(TAG, "leaveChat first BACK failed", e)
                }
            }
            svc.tickHandler.postDelayed({
                try {
                    val root = svc.rootInActiveWindow
                    val pkg = root?.packageName?.toString()
                    if (pkg != "com.whatsapp") return@postDelayed
                    val stillInChat =
                        !root.findAccessibilityNodeInfosByViewId("com.whatsapp:id/entry").isNullOrEmpty()
                    if (stillInChat) {
                        Log.i(TAG, "leaveChat: still in chat, BACK again")
                        svc.performGlobalAction(GLOBAL_ACTION_BACK)
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "leaveChat second BACK failed", e)
                }
            }, 450L)
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
                    attemptSend(root)
                    if (!armed.get()) return
                }
            } catch (e: Exception) {
                Log.w(TAG, "send tick failed", e)
            }
            tickHandler.postDelayed(this, 800L)
        }
    }

    private fun scheduleSendTicks() {
        cancelSendTicks()
        lastMediaSendClickAt = 0L
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

    /** When true, a11y must not write chat entry — only media caption. */
    @Volatile
    var pendingMedia: Boolean = false

    @Volatile
    var callback: ((Boolean, String?) -> Unit)? = null

    fun onSendAttempted(ok: Boolean, error: String?) {
        val cb = callback
        callback = null
        pendingJobId = null
        pendingText = null
        pendingMedia = false
        SendAccessibilityService.disarm()
        cb?.invoke(ok, error)
    }
}
