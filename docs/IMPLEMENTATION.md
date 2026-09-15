# 实现流程文档

> 对应需求：[REQUIREMENTS.md](./REQUIREMENTS.md)  
> 目标机：Pixel 7 Pro · Android 17  
> 状态：draft（实现蓝图，非已交付代码）

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────┐
│  Notion                                                 │
│  · 任务表（待发/已发/已回）                               │
│  · 消息流水表（统一事件）                                 │
└──────────────────────────┬──────────────────────────────┘
                           │ API
                           ▼
┌─────────────────────────────────────────────────────────┐
│  编排层（推荐 n8n，可自建小服务）                          │
│  · 拉取待发任务                                          │
│  · 下发「发送指令」到手机                                  │
│  · 接收手机上报的入站/出站事件 → 写回 Notion               │
└──────────────────────────┬──────────────────────────────┘
                           │ HTTP / WebSocket
                           ▼
┌─────────────────────────────────────────────────────────┐
│  Pixel 7 Pro（本机）                                      │
│  ┌─────────────────────┐  ┌──────────────────────────┐  │
│  │ WhatsApp 执行器      │  │ SMS 执行器 + 监听         │  │
│  │ AutoJs6 / Hamibot   │  │ 系统 SMS + 网关或 Tasker  │  │
│  │ 或 Tasker+AutoInput │  │ （Saily eSIM 本机线路）   │  │
│  └──────────┬──────────┘  └────────────┬─────────────┘  │
│             │ UI 模拟点击                │ Telephony/SMS │  │
│             ▼                            ▼               │
│      WhatsApp App                 系统电话 / Messages     │
└─────────────────────────────────────────────────────────┘
```

### 为什么这样拆

| 层 | 职责 |
|---|---|
| Notion | 任务与可读流水，人看、可改状态 |
| 编排 | 调度、重试、限速、字段归一 |
| 本机 WA | **只**模拟 App 操作，不挂 Web |
| 本机 SMS | 走系统短信 = 可用 Saily 原生线路 |

---

## 2. 技术选型（推荐组合）

| 能力 | 首选 | 备选 |
|---|---|---|
| WA 发送（UI） | AutoJs6 或 Hamibot 脚本 | Tasker + AutoInput；MacroDroid WhatsApp Send |
| WA 监听 | 通知监听（AutoNotification / 自写 NotificationListener / AutoJs 通知事件） | 打开聊天读屏（慢、脆） |
| SMS 发送/监听 | Android SMS Gateway（如 sms-gate.app）指定 Saily 订阅 | Tasker 收发 + HTTP 上报 |
| 编排 | n8n（自托管） | 极简 Flask/FastAPI |
| 任务/流水 | Notion Database | — |

**明确排除本期：** Evolution / Baileys / 扫码 Web 工具。

---

## 3. 分阶段实施步骤

### Phase 0 — 环境就绪（半天）

1. 确认 Pixel 7 Pro · Android 17；Saily eSIM 已激活。  
2. 设置 → 网络和互联网 → SIM：Saily 可用于**通话与短信**；默认短信 SIM 选 Saily。  
3. 拨号 `*#06#` 确认有 **EID**；系统 Messages 用 Saily 号实测收发 1 条短信。  
4. 安装 WhatsApp，用 Saily 号登录；关闭不必要的电池限制（后续自动化 App 同理）。  
5. 安装自动化运行时：AutoJs6 **或** Hamibot；按需安装 Tasker。  
6. 授予：无障碍、通知使用权、短信（若用网关）、后台运行、关闭电池优化。  
7. Android 13+ 若提示受限设置：应用信息 → 允许受限设置 → 再开无障碍。

**出口标准：** 人手能用 Saily 号打 WhatsApp、收发短信；自动化 App 无障碍开关为开。

---

### Phase 1 — 统一事件模型（与 Notion 表）

创建两个 Notion Database（名称可改）：

#### 1.1 任务表 `Outreach Tasks`

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| Name | Title | 任务标题 |
| Phone | Phone / Text | E.164，如 +1… |
| Contact Name | Text | 可选 |
| Channel | Select | `whatsapp` / `sms` |
| Message | Text | 待发正文 |
| Status | Select | `pending` / `sending` / `sent` / `replied` / `failed` / `cancelled` |
| Sent At | Date | |
| Replied At | Date | |
| Last Reply | Text | 最近回复摘要 |
| Error | Text | 失败原因 |

#### 1.2 消息流水 `Message Events`

| 字段 | 类型 | 说明 |
|---|---|---|
| Name | Title | 自动生成：`WA inbound +1…` |
| Channel | Select | `whatsapp` / `sms` |
| Direction | Select | `outbound` / `inbound` |
| Peer | Text | 对方号码或显示名 |
| Body | Text | 内容 |
| Occurred At | Date | 含时间 |
| Task | Relation | → Outreach Tasks |
| Raw | Text | 可选，原始 JSON |

**出口标准：** 手工能在 Notion 建任务、看流水；API Token 可读写这两张表。

---

### Phase 2 — SMS 通路（优先打通，比 WA 稳）

因 Saily 走**本机短信**，SMS 应最先做成可编程通道。

#### 步骤

1. 安装 SMS Gateway 类 App（例如 SMS Gateway for Android / httpSMS），授权短信权限。  
2. 确认发送时使用的是 **Saily** 订阅（双卡时必须选对 SIM）。  
3. 配置：  
   - `POST /send` ← 编排层调用发短信  
   - 入站 webhook → 编排层 URL  
4. n8n 流程 A：`Notion pending+sms` → 调网关发送 → 写 Message Events(outbound) → 任务 `sent`。  
5. n8n 流程 B：入站 webhook → 归一化 → 写 Message Events(inbound) → 按 Phone 匹配任务 → `replied`。

**出口标准：** 验收标准中的 SMS 两条全部通过。

---

### Phase 3 — WhatsApp 发送（本机 UI）

#### 推荐脚本流程（AutoJs6 / Hamibot）

1. 编排层对手机暴露指令，例如：  
   `POST http://手机或中继/wa/send { phone, text, task_id }`  
   （手机可用局域网 + 中继，或 Tasker HTTP 触发。）  
2. 脚本执行：  
   1. 亮屏 / 确保解锁（按选定策略）  
   2. 打开 `https://wa.me/<phone>?text=<urlencoded>` 或 Intent 调起 WhatsApp  
   3. 等待聊天页与输入框  
   4. 点击发送按钮（控件 id 常见为 `com.whatsapp:id/send`，**以真机层级为准，做成可配置**）  
   5. 回传成功/失败给编排层  
3. 编排层写 outbound 事件并更新任务状态。

#### 限速建议（初值）

- 单次发送间隔 ≥ 30–60s  
- 每日自动外联上限可配置（如 20）  
- 默认「仅向任务里已有号码发送」，不做通讯录盲扫  

**出口标准：** Notion 一条 WA 任务能自动发出并留痕。

---

### Phase 4 — WhatsApp 入站监听

#### 步骤

1. 开启通知使用权；WhatsApp 通知显示内容需包含发件人与消息预览（关闭「消息内容隐藏」类隐私若挡监听）。  
2. 监听通知包名 `com.whatsapp`（或 Business 包名）：解析 title≈联系人，text≈正文，timestamp。  
3. 上报编排层 → 写 inbound 事件 → 匹配 Phone/Name → 更新任务。  
4. 去重：同一 notification key / 内容+时间窗口内不重复入库。

**已知缺口：** 用户在 App 内已读且不弹通知时可能漏；后续可加「定时打开未读」兜底（Phase 5）。

**出口标准：** 对方回一条 WA，Notion 能看到谁/何时/内容。

---

### Phase 5 — 串联与加固

1. 统一 n8n：任务轮询 + WA/SMS 双通道 + 入站合流。  
2. 失败重试、告警（Telegram/邮件可选）。  
3. 选择器配置外置（WA 改版只改配置）。  
4. 可选：人工确认队列（高风险联系人先推送到手机确认再发）。  
5. 文档补充：真机权限截图清单、限速参数、故障排查。

---

## 4. 端到端主流程（运行时）

### 4.1 出站（任务 → 发送）

```
Notion 任务 Status=pending
    → 编排拉取
    → Channel?
         ├─ sms  → SMS Gateway 发送（Saily 线路）
         └─ whatsapp → 触发本机 UI 脚本发送
    → 写 Message Events(outbound)
    → 任务 Status=sent（失败则 failed + Error）
```

### 4.2 入站（回复 → 落库）

```
WA 通知 或 系统 SMS
    → 本机采集
    → POST 事件 { channel, peer, body, occurred_at }
    → 编排归一化
    → 写 Message Events(inbound)
    → 查找匹配任务（同 Phone + sent/pending）
    → 更新 Status=replied, Last Reply, Replied At
```

---

## 5. 本机权限清单（Pixel 7 Pro）

| 权限/设置 | 用于 |
|---|---|
| 无障碍 | WA UI 点击 |
| 通知使用权 | WA（及部分）入站 |
| 短信 / 电话（网关） | SMS 收发 |
| 显示在其他应用上层（若需要） | 部分自动化稳定性 |
| 忽略电池优化 | 常驻监听 |
| 锁屏策略 | UI 发送时能操作前台 |

---

## 6. 目录规划（代码落地后）

```
pixel-wa-sms-automation/
├── README.md
├── docs/
│   ├── REQUIREMENTS.md
│   └── IMPLEMENTATION.md
├── notion/                 # 可选：数据库字段说明、导入模板
├── n8n/                    # 工作流导出 JSON
├── android/
│   ├── autojs/             # WA 发送/辅助脚本
│   └── notes/              # 权限与机型备注
└── gateway/                # 可选：若不用现成 SMS Gateway，自写上报服务
```

---

## 7. 建议实施顺序（checklist）

- [ ] Phase 0 环境与 Saily 本机短信实测  
- [ ] Phase 1 Notion 两表 + Token  
- [ ] Phase 2 SMS 收发 + 同步 Notion  
- [ ] Phase 3 WA UI 发送 + 同步  
- [ ] Phase 4 WA 通知监听 + 同步  
- [ ] Phase 5 限速、去重、告警、选择器配置化  

---

## 8. 决策记录

| 决策 | 选择 | 原因 |
|---|---|---|
| WA 通道 | 本机 App UI，不挂 Web | 曾被第三方 Web 封关联设备 |
| 号码 | Saily eSIM + 本机电话/短信 | 用户已购；可走系统 SMS 自动化 |
| 设备 | Pixel 7 Pro / Android 17 | 用户指定；原生 eSIM + 后台相对友好 |
| 同步 | Notion | 用户目标落点 |
| 编排 | n8n 优先 | 少代码、webhook 友好 |
