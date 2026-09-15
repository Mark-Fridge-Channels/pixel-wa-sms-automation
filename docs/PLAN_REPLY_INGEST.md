# SMS / WhatsApp → Reply 回写完整串联方案

> 更新：2026-09-14  
> 接口文档：[Reply 回写文档](https://app.notion.com/p/3db9166fd9fd80629ff1c3befe56c660)  
> 端点：`POST /api/replies`（本地示例 `http://127.0.0.1:5173/api/replies`）  
> 鉴权：`Authorization: Bearer <REPLY_INGEST_TOKEN>`（未配置时用 `local-reply-ingest`）

## 1. 目标

把 Pixel 编排链路接到 Portal Reply 回写：

1. 从 TaskDB 领取当天 Pending（SMS / WhatsApp）  
2. 发送成功后 Task=`Completed`，Conversation=`Sent`  
3. 监听到客户回复后，调用 `POST /api/replies`（**不要**把 Gateway/Companion 原始 webhook 直接丢给 Portal）  
4. 回写所需关键字段在**领取/分配/执行前或发送成功瞬间**写入本地缓存，入站时只读缓存 + 回复正文

## 2. 接口怎么调（SMS / WhatsApp）

### 2.1 公共

| 项 | 值 |
|---|---|
| Method | `POST /api/replies` |
| Auth | `Bearer <REPLY_INGEST_TOKEN>` |
| 预检（可选） | `GET /api/replies/target?taskId=…&threadId=…` |
| 成功 | `201` 新建；同 `messageId` 再提交 → `200` + `duplicate: true` |
| 常见失败 | `409` Task/Thread 对不上或未发出；`422` 无已发出记录；`401` 鉴权 |

前置条件（文档强制）：

- 对应 Outbound Conversation：`Message Status = Sent`  
- 对应 Task：`Task Status = Completed`  
- 入站 `taskId` + `threadId` 必须与该条 Outbound **同一对**

### 2.2 SMS 请求体

```json
{
  "taskId": "<Follow-up Task 页面 ID>",
  "threadId": "<系统线程 ID，与 Outbound Conversation.Thread ID 相同>",
  "content": "<回复原文>",
  "channel": "SMS",
  "sender": "<可选，E.164；空则 Portal 补联系人手机号>",
  "messageId": "",
  "occurredAt": "<ISO8601 真实收到时间>",
  "extendedParameters": {
    "smsMessageSid": "<Gateway message id，可选>",
    "smsFrom": "<来信号码 E.164>"
  }
}
```

注意：

- `channel` 固定 `"SMS"`，不要写 WhatsApp  
- **`messageId` 回写留空**（服务端生成 `IN-SMS-{timestamp}`）  
- 供应商/Gateway ID 只进 `extendedParameters`，**不能**当 `threadId`

### 2.3 WhatsApp 请求体

```json
{
  "taskId": "<Follow-up Task 页面 ID>",
  "threadId": "<系统线程 ID，与 Outbound Conversation.Thread ID 相同>",
  "content": "<回复原文>",
  "channel": "WhatsApp",
  "sender": "<可选，E.164>",
  "messageId": "",
  "occurredAt": "<ISO8601>",
  "extendedParameters": {
    "whatsappMessageId": "<可选；本机无 wamid 时可省略或放本地指纹>",
    "whatsappConversationId": "whatsapp:+1…"
  }
}
```

注意：

- `channel` 固定 `"WhatsApp"`  
- **`messageId` 留空**  
- 通知 title / 原始包名会话 ID 进 `extendedParameters`，不是 `threadId`

## 3. 必须提前缓存的字段

按文档，入站时 Portal 只认 **taskId + threadId**（系统线程），匹配规则是「该通道该号码最近一次成功发出」。

### 3.1 缓存键

```
(channel, phone_e164) → reply_target
```

通道分离（与现有 `last_outbound_by_phone_{sms|whatsapp}.json` 一致），避免 SMS/WA 交叉污染。

### 3.2 缓存写入时机（推荐）

| 时机 | 写什么 | 原因 |
|---|---|---|
| **领取并 resolve 成功后、真正发送前** | 预写 `pending`：`taskId`、`conversationId`、`contactId`、`phone`、`channel`、**`threadId`** | 用户要求「执行前缓存」；入站极快回复时也能命中 |
| **发送成功、Task→Completed 后** | 覆盖为 `ready`：同上 + `sentAt`；标记可回写 | Portal 要求 Sent+Completed；失败则标 `failed` 且**不可**调 replies |
| 发送失败 | 清除或标 `failed`，禁止入站匹配该 pending | 避免 409/422 |

### 3.3 缓存结构（建议）

```json
{
  "+15551234567": {
    "channel": "SMS",
    "state": "ready",
    "task_page_id": "xxxxxxxx-…",
    "thread_id": "THR-…-SMS",
    "conversation_page_id": "yyyyyyyy-…",
    "contact_page_id": "zzzzzzzz-…",
    "phone_e164": "+15551234567",
    "our_number": "+18207863604",
    "sent_at": "2026-09-14T…Z"
  }
}
```

WhatsApp 文件同结构，`thread_id` 形如 `THR-…-WhatsApp`，`channel=WHATSAPP`。

### 3.4 `threadId` 来源（关键）

文档：`threadId` = **系统线程 ID**，来自该 Task 关联 Conversation 的 `Thread ID`（可能形如 `THR-…-SMS` / `THR-…-WhatsApp`）。  
**不是**按手机号生成的键，也不是 Gateway SID / wamid。

重要澄清：

- `threadId` **不是**「按人」的稳定 ID，**会随 Task/Conversation 变化**  
- 编排侧 **只拷贝** Conversation 上已有的 `Thread ID` 进缓存；**禁止本地发明**（含 `sms:+e164` / `wa:+e164`）  
- resolve 时若 Conversation 缺少 `Thread ID` → `resolve_error`，该 Task 不发送、不写 ready  
- `(channel, phone)` 仅用于**查找**「该通道该号最近一次成功发出」对应哪条缓存；真正回写用缓存里的 `taskId` + `threadId`

入站匹配只用 `get_ready_for_reply()` 返回的缓存；**禁止**调用已废弃的 `thread_id_for_phone()` 拼 Portal `threadId`。

## 4. 端到端流程

```
Notion TaskDB (Pending, Channel=SMS|WhatsApp, Scheduled At=today NY)
        │
        ▼
orchestrator plan-today / run-scheduler
        │  resolve: Contact→KeyPerson→Phone；Conversation.Content；读 Thread ID
        │  cache.set(pending: taskId, threadId, phone, channel, …)
        ▼
发送执行
  SMS  → Gateway simNumber=2
  WA   → Companion job 队列 → 无障碍发送
        │
        ├─ 失败 → Task Failed；cache 不可 replies
        └─ 成功 → Task Completed；Conversation Sent；cache.set(ready)
        ▼
入站监听
  SMS  → Gateway webhook /webhook/sms
  WA   → Companion 通知 / 发后读屏 → /webhook/whatsapp
        │
        ▼
handle_inbound_*
  1. normalize phone + content + occurredAt
  2. cache.get(channel, phone) → taskId, threadId
  3. （可选）GET /api/replies/target 预检
  4. POST /api/replies  （messageId=""；channel 固定；extendedParameters 放供应商字段）
  5. 本地可选再写 Notion Inbound Conversation（或改为仅依赖 Portal 回写，二选一避免双写）
```

### 4.1 落地状态（已实现）

| 模块 | 状态 |
|---|---|
| `OutboundCache` | `pending` / `ready` / `failed` + `thread_id`；入站仅 `ready` 且含 task+thread |
| `notion_client.resolve` | 读取 Conversation.`Thread ID`；缺失则 `resolve_error` |
| `outbound` | 发送前 `set_pending`，成功 `set_ready`，失败 `mark_failed`；WA job 携带 `thread_id` |
| `ReplyWebhookClient` | `build_sms_payload` / `build_whatsapp_payload` + `post_reply`；强制 `messageId=""` |
| `inbound` | 匹配后调 Portal；供应商 ID 仅进 `extendedParameters` |
| `thread_id_for_phone` | 保留但标注 Deprecated，Reply 路径不使用 |
| 配置 | `REPLY_WEBHOOK_URL` → 完整 `/api/replies`；`REPLY_WEBHOOK_TOKEN`（默认 `local-reply-ingest`） |

### 4.2 同号 / 双通道

- 缓存按通道分文件：同号 SMS 与 WA 互不覆盖  
- 入站按通道选缓存（SMS webhook → sms cache；WA → wa cache）  
- （后续）设备忙锁 / 同号互斥仍建议，但不阻塞本 Reply 串联

## 5. 参数映射速查

| Reply 字段 | SMS 来源 | WhatsApp 来源 |
|---|---|---|
| `taskId` | 缓存 `task_page_id` | 同左 |
| `threadId` | 缓存 `thread_id`（= Outbound Conversation） | 同左 |
| `content` | webhook body | 通知/读屏正文 |
| `channel` | `"SMS"` | `"WhatsApp"` |
| `sender` | 来信号码 E.164 | 来信号码（读屏已知 phone；通知可能仅姓名→尽量用缓存 phone） |
| `messageId` | **空** | **空** |
| `occurredAt` | Gateway `receivedAt` | 通知 `postTime` 或读屏刮到时刻 |
| `extendedParameters` | `smsMessageSid`, `smsFrom` | `whatsappConversationId`=`whatsapp:{e164}`；本地指纹可选 |

## 6. 测试计划

### T0 契约 / 单测（无真机）

1. 缓存 pending→ready→failed 状态机  
2. `ReplyWebhookClient` payload：SMS/WA 字段齐全；`messageId` 为空；channel 字符串正确  
3. 未 ready 的缓存不调用 replies  
4. Mock Portal：`201` / `200 duplicate` / `409` / `422` 处理与日志  

### T1 出站前置（Notion 沙箱 Task）

1. 准备 Channel=SMS / WhatsApp 各 1 条 Pending；Conversation 带明确 `Thread ID`（Portal 格式）  
2. dry-run / 真发：确认缓存写入含 `taskId`+`threadId`  
3. 发送成功后：`GET /api/replies/target?taskId&threadId` 返回可挂靠  

### T2 SMS 入站 → replies

1. 真机发出 SMS Task 成功（若运营商投递仍 66，用已 Delivered 的目标号或先 mock Sent）  
2. 对方回复 → Gateway webhook  
3. 断言：`POST /api/replies` 发出且 `channel=SMS`；ConversationDB 出现 Inbound / Needs Reply  
4. 负例：错误 channel 写成 WhatsApp → 不应匹配；无缓存 → 不调用或记 unmatched  

### T3 WhatsApp 入站 → replies

1. WA Task 发出成功  
2. 通知入站 + 发后读屏入站各测一次  
3. 断言：`channel=WhatsApp`；`occurredAt` 有值；`messageId` 未传业务 ID  
4. 去重：同一回复通知+读屏只成功写入一次（Portal duplicate 或本地指纹）  

### T4 交叉

1. 同号先 SMS 后 WA（或反向）：入站各走各通道缓存  
2. 发送失败的 Task：对方仍回信 → 不得用失败 task 调 replies  

### T5 验收标准

| # | 标准 |
|---|---|
| A | 出站成功后缓存必有 `taskId` + `threadId` |
| B | 入站调用 replies 的 body 符合文档；`messageId` 为空 |
| C | Portal ConversationDB 挂到正确 Task/Thread；`Reply Status=Needs Reply` |
| D | SMS 与 WhatsApp 互不串通道 |
| E | pytest 覆盖缓存与 payload；真机各至少 1 条 SMS、1 条 WA 闭环（SMS 受运营商限制时可对 Portal 用 fixture Sent） |

## 7. 实施顺序

1. ~~对齐 `threadId`（只读 Conversation，缺失即失败）~~  
2. ~~扩展缓存 + 发送前后写入~~  
3. ~~重写 `ReplyWebhookClient` + `inbound`~~  
4. 配置 Portal `REPLY_WEBHOOK_URL` / `REPLY_WEBHOOK_TOKEN`  
5. 跑 T1–T5 真机 / Portal 联调  

## 8. 待确认（联调前）

1. Portal 现网 / 本地 base URL（文档示例 `:5173`）与生产 token  
2. 待测 Task 的 Conversation 是否已有可用 `Thread ID`  
3. 入站是否仍双写本地 Notion Conversation（当前默认：有 token 时仍写本地；主路径以 Portal `/api/replies` 为准）
