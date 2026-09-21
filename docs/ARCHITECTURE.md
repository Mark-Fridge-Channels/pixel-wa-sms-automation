# 完整架构与每日执行流程

> 更新：2026-09-15  
> 范围：SMS + WhatsApp + Email（Gmail）  
> 代码：`channel-orchestrator/` · `wa-companion/` · 手机 SMS Gateway  
> 核对用：请对照本页图与真实部署是否一致

---

## 0. 现状说明（给你 check）

| 文档 | 有没有图 | 是否覆盖三通道 + 每日执行 |
|---|---|---|
| [PLAN.md](./PLAN.md) | ASCII，偏 SMS | 否（缺 Email、缺统一日流程） |
| [PLAN_WHATSAPP.md](./PLAN_WHATSAPP.md) | ASCII | 仅 WA |
| [API_WA_PROBE.md](./API_WA_PROBE.md) | 无 | WA 号码探测 API（不走 Task） |
| [PLAN_EMAIL.md](./PLAN_EMAIL.md) | 文字 + 前置 | 仅 Email |
| [PLAN_REPLY_INGEST.md](./PLAN_REPLY_INGEST.md) | 文字流程 | Reply 契约，非全天调度 |
| [IMPLEMENTATION.md](./IMPLEMENTATION.md) | 早期蓝图 | **过时**（n8n / AutoJs 等） |
| **本文 ARCHITECTURE.md** | Mermaid 全图 | **是：三通道架构 + 每天怎么跑** |

---

## 1. 总体架构

```mermaid
flowchart TB
  subgraph notion [Notion FC3.0]
    TaskDB["TaskDB Pending\nChannel=SMS|WhatsApp|Email"]
    ConvDB["ConversationDB\nOutbound / Inbound"]
  end

  subgraph portal [Portal]
    Replies["POST /api/replies"]
  end

  subgraph server [channel-orchestrator 常开]
    Sched["run-scheduler\n日计划 + 到点执行"]
    Serve["serve :8787\nwebhook + WA jobs"]
    Cache["OutboundCache\nsms / whatsapp / email"]
    Gmail["GmailClient\nOAuth + history poll"]
  end

  subgraph phone [Pixel 手机]
    Gateway["SMS Gateway App"]
    Companion["WA Companion"]
    WAApp["WhatsApp App"]
    Messages["系统 Messages / Saily"]
  end

  TaskDB --> Sched
  Sched -->|SMS send| Gateway
  Sched -->|enqueue wa_jobs| Serve
  Companion -->|poll jobs / result| Serve
  Companion --> WAApp
  Gateway --> Messages
  Sched -->|Email send/reply| Gmail
  Gmail -->|history inbound| Sched
  Gateway -->|POST /webhook/sms| Serve
  Companion -->|POST /webhook/whatsapp| Serve
  Serve --> Cache
  Sched --> Cache
  Cache --> Replies
  Replies --> ConvDB
  Sched -->|Task Completed/Failed\nConv Sent + Extended Parameters| notion
```

### 执行面分工

| 通道 | 出站执行 | 入站监听 |
|---|---|---|
| SMS | 服务器调手机 **SMS Gateway** | Gateway → `POST /webhook/sms` |
| WhatsApp | Companion 轮询 **wa_jobs** → 本机 UI | 通知/读屏 → `POST /webhook/whatsapp` |
| Email | 服务器 **Gmail API** 直发 | 服务器 **history 轮询**（不经手机） |

---

## 2. 每天如何执行（核心）

时区默认 **`America/New_York`**。常驻命令：

```bash
cd channel-orchestrator
python3 -m channel_orchestrator.cli serve --port 8787          # 终端 A：API / webhook / WA jobs
python3 -m channel_orchestrator.cli run-scheduler --channel all # 终端 B：日计划 + 到点发送 + Gmail poll
```

### 2.1 一天时间线

```mermaid
flowchart LR
  subgraph morning [纽约当天开始]
    T0["00:00+\n按 NY 日期算 day"]
  end
  subgraph planWin [建日计划]
    T1["约 08:30 后\nDAILY_PLAN_HOUR/MINUTE\n或尚无 plan 文件时"]
    T2["拉取各通道 Pending\nScheduled At = 当天"]
    T3["按 P0→P1→P2\n在 09–12 / 14–18 排点\n写入 daily_plan_{channel}_{day}.json"]
  end
  subgraph workday [工作窗循环]
    T4["每 SCHEDULER_POLL_SECONDS\n约 20s 一轮 tick"]
    T5["due = planned 且 scheduled_at ≤ now"]
    T6["执行出站\nSMS / WA enqueue / Email"]
    T7["每 GMAIL_POLL_SECONDS\n约 30s 拉 Gmail history"]
  end
  T0 --> T1 --> T2 --> T3 --> T4 --> T5 --> T6
  T4 --> T7
```

### 2.2 每个 tick 实际做什么

```mermaid
flowchart TD
  Start[run-scheduler tick] --> LoopCh{对每个通道\nSMS / WHATSAPP / EMAIL}
  LoopCh --> NeedPlan{无 plan 或已过\n每日计划时刻?}
  NeedPlan -->|是| Build["build_daily_plan\nmerge 已 completed/failed/queued"]
  NeedPlan -->|否| Load[load_plan]
  Build --> Due
  Load --> Due["process_due\ndue_items"]
  Due --> Item{下一条 due}
  Item --> Recheck{Notion Status\n仍是 Pending?}
  Recheck -->|否| Skip[plan item = skipped]
  Recheck -->|是| Resolve[resolve Task\nContact→KP→phone/email\nConv Content + Thread ID\nEmail 另读 Extended Parameters]
  Resolve --> Branch{通道}
  Branch -->|SMS| SMS[Gateway send_sms]
  Branch -->|WhatsApp| WA[enqueue wa_jobs\nCompanion 稍后领取]
  Branch -->|Email| EM[Gmail send_new_or_reply]
  SMS --> CacheReady[cache ready\n+ Notion Completed/Sent]
  WA --> CachePending[cache pending\n成功回传后再 ready]
  EM --> CacheReady
  Start --> GmailPoll[若已配 Gmail\npoll_gmail_inbound]
  GmailPoll --> Match[缓存匹配]
  Match --> Portal["POST /api/replies"]
```

### 2.3 日计划文件（按通道分开）

| 文件 | 含义 |
|---|---|
| `data/daily_plan_sms_YYYY-MM-DD.json` | 当天 SMS 排程 |
| `data/daily_plan_whatsapp_YYYY-MM-DD.json` | 当天 WhatsApp 排程 |
| `data/daily_plan_email_YYYY-MM-DD.json` | 当天 Email 排程 |
| `data/wa_jobs.json` | WA 待发队列（Companion 消费） |
| `data/last_outbound_by_phone_{sms\|whatsapp\|email}.json` | 入站匹配缓存 |
| `data/gmail_history_state.json` | Gmail historyId 游标 |

### 2.4 工作窗与间隔（默认）

| 配置 | 默认 | 作用 |
|---|---|---|
| `TIMEZONE` | America/New_York | 「当天」与工作窗 |
| `WORK_WINDOWS` | 09:00-12:00,14:00-18:00 | 可排程时段（午休不发） |
| `MIN_SEND_INTERVAL_SECONDS` | 90 | SMS/Email 最小间隔 |
| `WA_MIN_SEND_INTERVAL_SECONDS` | 120 | WhatsApp 排程间隔 |
| `DAILY_PLAN_HOUR` / `MINUTE` | 8 / 30 | 之后会（重）建当日 plan |
| `SCHEDULER_POLL_SECONDS` | 20 | 调度轮询 |
| `GMAIL_POLL_SECONDS` | 30 | Email 入站轮询 |

---

## 3. 出站流程（按通道）

### 3.1 公共：resolve → 发送 → Notion / 缓存

```mermaid
sequenceDiagram
  participant Sched as run-scheduler
  participant Notion as Notion
  participant Cache as OutboundCache
  participant Exec as 执行器

  Sched->>Notion: get Task Status = Pending?
  Sched->>Notion: resolve Contact→KP, Conversation
  Note over Sched,Notion: 必须有系统 Thread ID\nEmail 另读 Extended Parameters
  Sched->>Cache: set_pending
  Sched->>Notion: Task = In Progress
  Sched->>Exec: 发送
  alt 成功
    Exec-->>Sched: ok + provider ids
    Sched->>Notion: Task Completed / Conv Sent
    Sched->>Cache: set_ready
  else 失败
    Sched->>Notion: Task Failed + Notes
    Sched->>Cache: mark_failed
  end
```

### 3.2 通道差异

| 步骤 | SMS | WhatsApp | Email |
|---|---|---|---|
| 收件人 | Key Person.`Phone` | 同左 | Key Person.`Email` |
| 正文 | Conversation.`Content` | 同左 | 同左 + Subject |
| 系统 Thread ID | Conversation.`Thread ID` | 同左 | 同左 |
| 供应商线程 | — | — | `Extended Parameters.gmailThreadId` 空=新开，有=跟帖 |
| 执行器 | Gateway HTTP | Companion 领 job | Gmail API |
| ready 时机 | Gateway 返回成功后 | Companion `result ok` 后 | Gmail send 成功后 |

---

## 4. 入站与 Reply 回写

```mermaid
flowchart TD
  subgraph sources [入站来源]
    S1[SMS Gateway webhook]
    S2[WA Companion webhook]
    S3[Gmail history poll]
  end

  subgraph match [匹配]
    M1["按通道选缓存文件"]
    M2["SMS/WA: phone → ready"]
    M3["Email: gmailThreadId 优先\n否则 From email"]
  end

  subgraph portal [Portal]
    P1["POST /api/replies\nmessageId 留空\ntaskId + threadId 来自缓存"]
  end

  S1 --> M1
  S2 --> M1
  S3 --> M1
  M1 --> M2
  M1 --> M3
  M2 --> P1
  M3 --> P1
  P1 --> Ext["Email: extendedParameters\n写入 Conversation\n供排班下次跟帖"]
```

**两个 ID 不要混：**

| 名字 | 是什么 | 用在哪 |
|---|---|---|
| `threadId` | 系统线程，如 `THR-…-SMS` | Portal `/api/replies`、Conversation.`Thread ID` |
| `gmailThreadId` | Gmail 线程 | 仅 `Extended Parameters`（及 Email 缓存） |

Email 入站：优先按 `gmailThreadId` 挂原 Task；From≠出站收件人时见 [PLAN_EMAIL_PROXY_REPLY.md](./PLAN_EMAIL_PROXY_REPLY.md)（同公司 `proxyReply` / 跨域 `unexpectedSender`）。

无 Task ready 时走冷进线 `POST /api/inbound`（Domain → `FollowUpClientId`），见 [PLAN_EMAIL_COLD_INBOUND.md](./PLAN_EMAIL_COLD_INBOUND.md)。

详情：[PLAN_REPLY_INGEST.md](./PLAN_REPLY_INGEST.md) · [PLAN_EMAIL.md](./PLAN_EMAIL.md)

---

## 5. 组件与端口一览

| 组件 | 地址 / 路径 | 职责 |
|---|---|---|
| Orchestrator API | `:8787` | `/webhook/sms` `/webhook/whatsapp` `/wa/jobs/*` `/health` |
| SMS Gateway | 常 `:8080` 或 adb reverse | 本机发短信 / 推入站 |
| Portal replies | 如 `:5173/api/replies` | 入站挂到 Task/Thread |
| Gmail API | googleapis.com | 发信 + history |

---

## 6. 建议你核对的清单

- [ ] 生产是否同时常开 `serve` + `run-scheduler --channel all`
- [ ] Notion 三通道 Pending 的 `Scheduled At` 是否按纽约「当天」
- [ ] Conversation 是否都有系统 `Thread ID`（缺则 resolve 失败不发）
- [ ] Email 新开线程 Extended Parameters 为空；跟帖任务已带 `gmailThreadId`
- [ ] SMS/WA 手机侧 Gateway / Companion 总开关 ON，能访问编排公网/内网
- [ ] `.env` 中 `REPLY_WEBHOOK_*`、`GMAIL_*`、`SAILY_PHONE_E164` 正确

有出入直接改本页或对应 PLAN，避免再散落过时 ASCII 图。
