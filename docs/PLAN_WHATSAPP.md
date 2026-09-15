# WhatsApp 本机自动化方案

> 更新：2026-09-14  
> 约束：禁止 WhatsApp Web / Baileys / 扫码关联设备；Notion 流程与 SMS 对齐，仅 `Channel=WhatsApp`

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
