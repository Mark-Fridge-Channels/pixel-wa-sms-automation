# WhatsApp 本机自动化方案

> 更新：2026-09-21  
> 约束：禁止 WhatsApp Web / Baileys / 扫码关联设备；Notion 流程与 SMS 对齐，仅 `Channel=WhatsApp`

专机建议：`adb shell pm grant com.pixelwa.companion android.permission.WRITE_SECURE_SETTINGS`，以及 `SEND_SMS`，以便 APK 更新后自动恢复无障碍、异常时短信告警。韧性验收见 [TEST_RESILIENCE.md](./TEST_RESILIENCE.md)。

## 结论

| 层 | 做法 |
|---|---|
| Notion / 编排 | 与 SMS 同一套日调度与回写；`Channel=WhatsApp` |
| 手机执行 | **WA Companion App**：无障碍发送 + NotificationListener 入站 |
| 连通 | 手机 **轮询服务器** 取 job、上报结果与入站（服务器不直连手机内网） |

## Notion 流程

与 SMS 相同：当天 Pending → Priority 工作窗排程 → 复核 → Contact→Key Person→Phone → 唯一 Conversation.`Content` → 更新 Task Status/Ended At → 更新 Conversation → 按号缓存 → 入站匹配。

差异：Thread ID = `wa:{e164}`；执行器为 Companion，非 SMS Gateway。

## 架构

```
Notion → orchestrator（服务器）→ wa_jobs 队列
                                      ↑
Pixel WA Companion ──轮询/回传/入站───┘
         │
    WhatsApp App（本机 UI）
```

## Companion 职责

1. 总开关 ON/OFF（通知栏）  
2. 轮询 `GET /wa/jobs/next` → `wa.me` + 无障碍点发送 → `POST /wa/jobs/{id}/result`  
3. 发送成功后在聊天内短驻留读屏；新气泡用**当前时间**作 `received_at` 上报（补前台无通知）  
4. NotificationListener（`com.whatsapp`）→ `POST /webhook/whatsapp`（与读屏短窗去重）  
5. 插电、关电池优化、开机自启；充电时保持可操作屏幕  

## 媒体（方案 A）

| 方向 | 范围 | 约定 |
|---|---|---|
| 出站 | 仅 **image / video** | Conversation（或 Task 覆盖）`Extended Parameters`：`{"mediaUrl":"https://...","mediaType":"image\|video"}`；正文作 caption 可选 |
| 入站 | image / video / audio / file | Companion 读本地 WA 媒体 → `POST /webhook/whatsapp/media` → orch 上传 S3 → Portal `extendedParameters.mediaUrl` |

Companion 需授予通知监听、无障碍，以及 **All files access**（读入站媒体）。版本 ≥ **0.4.0**。

专机建议：`adb shell pm grant com.pixelwa.companion android.permission.WRITE_SECURE_SETTINGS`，以便 APK 更新后自动恢复无障碍。

## 稳定性（0.4.0）

- 轮询 FGS 从 `dataSync` 改为 **`specialUse`**，避开 Android 15 的 6h/24h 后台时长配额（此前会导致 `ForegroundServiceDidNotStopInTimeException`）。
- 实现 `onTimeout` 优雅停机；`ServiceWatchdog` 经通知监听每 45s 检查轮询/无障碍，失败则重启或告警。
- APK 覆盖安装会清空无障碍授权；有 `WRITE_SECURE_SETTINGS` 时可自动写回。

## 已知问题 / 待办

### WA 入站媒体：通知预览图 ≠ 原文件（2026-09-18）— 已修（0.3.2）

- **原现象**：入站 image 走通 S3，但常是通知 `largeIcon`（对方头像）或缩略图，不是原文件；音视频找不到文件时只剩文本。
- **修复（Companion 0.3.2）**：
  1. **禁止**用通知 Bitmap / `largeIcon` 当媒体上传；
  2. 识别到媒体后，用通知 `contentIntent` **打开对应聊天**触发 WA 下载；
  3. 再扫 `WhatsApp/Media/...`（排除 `Sent/`），校验最小文件大小后上传；
  4. 仍拿不到原件则只上报文本占位，不假装媒体成功。
- **设备侧仍需**：WA「媒体自动下载」打开；Companion「所有文件访问」已授权。
- **残余风险**：纯 caption、无关键字且无 `EXTRA_PICTURE` 的媒体通知可能仍只当文本；阅后即焚不可落盘。

### FGS dataSync 超时崩溃（2026-09-20）— 已修（0.4.0）

- **原现象**：Companion 后台跑满约 6h 后崩溃，`dataSync` 配额耗尽无法重启，出站 job 停摆。
- **修复**：`specialUse` + watchdog + 无障碍自检/可自动恢复。

## Gate G

| Gate | 内容 |
|---|---|
| G0 | 人工本机 WA 收发 OK |
| G1 | Companion 触发一条发送成功 |
| G2 | 通知入站到服务器 |
| G3 | Notion WhatsApp Pending 自动发 + Conversation Sent |
| G4 | 入站匹配 Task |
| G5 | 开关 OFF 不外发；断网恢复后轮询恢复 |

详见代码：`channel-orchestrator/`（WA API）、`wa-companion/`（Android）。
