# WA Companion 韧性与告警验收

目标：验证「昨晚 dataSync 崩溃」不会再发生，并覆盖重启 / VPN / 系统限制 / 短信告警。

## 前置

- Companion ≥ **0.4.1**（`specialUse` FGS）
- 已授：无障碍、通知监听、所有文件、**发短信**、电池优化白名单
- 专机建议：
  ```bash
  adb shell pm grant com.pixelwa.companion android.permission.WRITE_SECURE_SETTINGS
  adb shell pm grant com.pixelwa.companion android.permission.SEND_SMS
  adb shell appops set com.pixelwa.companion MANAGE_EXTERNAL_STORAGE allow
  ```
- Orchestrator `.env`：`ALERT_SMS_TO=+8615810494081`（默认已是该号）
- SMS Gateway 可发短信（服务器侧告警走 Gateway；手机侧本地告警走 `SmsManager`）

---

## A. 证明不会再被 dataSync 6h 杀掉

Companion 已改为 `foregroundServiceType=specialUse`，**不受** dataSync 6h 配额约束。

快速确认：

```bash
adb shell dumpsys activity services com.pixelwa.companion | rg "PollingForeground|types=|isForeground"
# 期望：isForeground=true 且 types=0x40000000（SPECIAL_USE）
```

可选「负向」证明：即便打开 dataSync 限时兼容开关，也不应再因 dataSync 崩：

```bash
adb shell am compat enable FGS_INTRODUCE_TIME_LIMITS com.pixelwa.companion
# 保持 Automation ON 观察；不应再出现 ForegroundServiceDidNotStopInTimeException(dataSync)
adb logcat -s WaPoll:V AndroidRuntime:E | rg "dataSync|DidNotStopInTime|WaPoll"
```

缩短 dataSync 超时（仅测旧类型时有用；specialUse 应无视）：

```bash
adb shell device_config put activity_manager data_sync_fgs_timeout_duration 120000
```

---

## B. 模拟与自动恢复清单

### 1) 手机重启

```bash
adb reboot
# 开机后
adb shell dumpsys activity services com.pixelwa.companion | rg PollingForeground
adb shell settings get secure enabled_accessibility_services
# 期望：Polling 在跑；无障碍仍启用（WRITE_SECURE_SETTINGS + BootReceiver）
```

### 2) VPN 节点异常

- 在青山断开 / 换坏节点，或 `adb shell am force-stop com.wieifjyr.qs2`
- 期望：Companion VPN watchdog 尝试重连；心跳带 `google_ok=false`
- 若持续失败 ≥ 冷却：手机发告警短信；服务器侧 grace 后也会经 SMS Gateway 告警

### 3) Android 限制（杀轮询 / 清无障碍）

**杀轮询进程（看门狗应拉起）：**

```bash
adb shell am stopservice com.pixelwa.companion/.PollingForegroundService
# 等 ≤45s（通知监听 health tick）
adb shell dumpsys activity services com.pixelwa.companion | rg PollingForeground
adb logcat -d -s WaWatchdog:V WaPoll:V | tail -30
```

**模拟 APK 更新清无障碍：**

```bash
adb shell settings delete secure enabled_accessibility_services
adb shell settings put secure accessibility_enabled 0
# 触发恢复：重建 Companion 界面，或等通知监听 45s health tick
adb shell am start -S -n com.pixelwa.companion/.MainActivity
adb shell settings get secure enabled_accessibility_services
# 期望：自动写回 com.pixelwa.companion/...SendAccessibilityService
```

**强制停止整包后拉活：**

```bash
adb shell am force-stop com.pixelwa.companion
adb shell am start -n com.pixelwa.companion/.MainActivity
# 期望：Polling isForeground + a11y Bound；长时间不打开则服务器 heartbeat_stale → 短信
```

### 4) 持续异常 → 短信 `+86 15810494081`

两条路径（互补）：

| 路径 | 触发 | 通道 |
|---|---|---|
| 手机本地 | 无障碍/轮询连续失败约 **15 分钟** 仍恢复不了；或 VPN 恢复失败 | `SmsManager` → 本机 SIM |
| 服务器 | heartbeat 超时 / `poll_alive=false` / `a11y_bound=false` / `google_ok=false` 持续超过 **ALERT_UNHEALTHY_GRACE_MINUTES**（默认 20） | SMS Gateway → `ALERT_SMS_TO` |

冷却：默认约 **60 分钟** 不重复狂发。

**缩短 grace 做联调（服务器）：**

```bash
# .env
ALERT_SMS_TO=+8615810494081
ALERT_UNHEALTHY_GRACE_MINUTES=2
ALERT_SMS_COOLDOWN_MINUTES=10
DEVICE_HEARTBEAT_STALE_MINUTES=2
```

然后：

```bash
adb shell am force-stop com.pixelwa.companion
# 等 2–3 分钟，看 orchestrator 日志 / 手机是否收到告警短信
```

**手机本地快速测短信权限（不发业务告警）：** 在 Companion 打开时允许 SMS；或：

```bash
adb shell pm grant com.pixelwa.companion android.permission.SEND_SMS
```

---

## C. 一键健康快照

```bash
adb shell dumpsys package com.pixelwa.companion | rg "versionName|WRITE_SECURE|SEND_SMS"
adb shell settings get secure enabled_accessibility_services
adb shell dumpsys activity services com.pixelwa.companion | rg "PollingForeground|isForeground|types="
adb logcat -d -s WaPoll:V WaWatchdog:V WaA11yRestore:V WaAlertSms:V WaVpnWatchdog:V | tail -50
curl -sS "$ORCH/health"   # 或 monitor summary 看 phone_stale / heartbeat
```
