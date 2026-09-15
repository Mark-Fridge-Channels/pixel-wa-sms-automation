# 完整方案：SMS → WhatsApp → Email

> 更新：2026-09-15  
> 设备：Pixel 7 Pro · Android 17 · Saily eSIM（本机电话/短信）  
> Notion：**不新建表**，对接既有 FC3.0 库  
> **完整架构图与「每天如何执行」见 [ARCHITECTURE.md](./ARCHITECTURE.md)**

---

## 0. 结论先说

| 问题 | 答案 |
|---|---|
| 怎么实现？ | 手机 = 短信/WhatsApp **执行器**；电脑/小服务 = **编排**（读 Task、写 Conversation） |
| 稳定性怎么保证？ | 分阶段验证：先「能发能听」→ 再「充电常驻 / 唤醒 / 断网恢复」→ 再 7×24 浸泡 |
| 一直充电 7×24 可行吗？ | **可行**（专用机强烈推荐）。充电下 Doze 限制大幅放宽；再配合关电池优化 + 前台服务 + 心跳 |
| 本期顺序 | **只做 SMS** → 验收通过 → 再做 WhatsApp UI |
| **最终运行形态** | **不依赖 USB**；手机上有 **总开关**，可随时开/关自动化 |

**不做：** 扫码挂 WhatsApp Web；不为 Notion 新建表。

---

## 0.1 最终效果（产品形态）— 已对齐

你要的不是「开发时用 USB 遥控手机」，而是：

1. **日常运行：手机可以独立工作**  
   - 不插电脑也能收发短信、执行任务、回写 Notion  
   - USB 只用于开发期安装/调试，不是生产依赖  

2. **手机上有明确的「自动化总开关」**  
   - **开**：监听入站 + 接受出站任务（或按策略只开其中一项）  
   - **关**：立刻停止自动发送与自动处理；人工用手机不受影响  
   - 一眼能看出当前是 ON / OFF（通知栏常驻「自动化运行中」更佳）  

3. **控制权在手机侧**  
   - 拔掉 USB、离开电脑，开关仍然有效  
   - 关开关后，编排层即使下发任务也应被拒绝或排队不发  

### 推荐实现（SMS 阶段）

| 层 | 职责 |
|---|---|
| 手机 App（Gateway / 控制台） | 总开关、前台服务、本机发短信、收短信、上报事件 |
| 编排服务（可跑在家里 NAS/云/常开电脑） | 轮询 Notion Task、在开关=ON 时才下发发送指令、写 Conversation |
| 通信 | **Wi‑Fi / 蜂窝 HTTPS**（Cloud 或内网），不是 USB |

开发期可用 USB 装 App、看日志；**验收标准必须包含：拔掉 USB 后，仅靠手机开关 + 网络仍能跑完整 SMS 闭环。**

---

## 1. 已有 Notion 对接（只读映射）

### 1.1 Task：`FC3.0-Follow-up-TaskDB`

- URL：https://app.notion.com/p/ff50607a3fdd469eb5fc2592a33ff63b  
- Data source：`collection://79601071-9560-41c1-9ad2-a036bd4c2df8`

| 字段 | 用途 |
|---|---|
| Channel = `SMS` / `WhatsApp` / `Email` | 选通道（Email 由服务器 Gmail API 执行，见 `docs/PLAN_EMAIL.md`） |
| Task Status = `Pending` | 可拉取执行 |
| Follow-up Contact | → Contact → Key Person |
| Template | 可选，取发送文案 |
| Notes | 失败原因（中文） |
| Ended At | 完成/失败时写入 |

### 1.2 回写：`FC3.0-Follow-up-ConversationDB`

- URL：https://app.notion.com/p/a7ded397b23f47f1a9113855fc3d10ce  
- Data source：`collection://0fe672f1-49c6-498b-a4c6-d5c5fe6d3fa4`

| 字段 | 出站 SMS | 入站 SMS |
|---|---|---|
| Channel | SMS | SMS |
| Direction | Outbound | Inbound |
| Content | 正文 | 正文 |
| Sender | Saily 本机号 | 对方号码 |
| Message Status | Sent / Failed | Received |
| Interaction At | 发送时间 | 收到时间 |
| Follow-up Task | 关联任务 | 能匹配则关联 |
| Follow-up Contact | 关联联系人 | 按号码匹配 |
| Thread ID | `sms:{规范化手机号}` | 同左 |
| Message ID | 网关/本地生成 ID | 同左 |
| Reply Status | — | Needs Reply（入站） |

### 1.3 号码来源

- Contact → **Key Person**（`FC3.0-KeyPersonDB`）上的 **Phone** 字段  
- 发送前必须解析到 E.164 手机号，否则任务 Failed + Notes

---

## 2. 目标架构（SMS 阶段）

```
Notion TaskDB (Channel=SMS, Status=Pending)
        │
        ▼
┌───────────────────┐   HTTPS/公网    ┌────────────────────────────┐
│ channel-orchestrator  │ ◄────────────► │ 手机 SMS Gateway App        │
│ (服务器常开)       │  发指令/收事件  │ (系统短信 API + Saily SIM)  │
└─────────┬─────────┘               └────────────────────────────┘
          │
          ▼
Notion ConversationDB  +  Task Status→Completed/Failed
```

开发期可用 Mac + USB/`adb forward`；**生产编排跑在服务器**，手机不跑 Notion 调度。

### 为什么 SMS 用「系统短信网关」而不是 UI 模拟

- 不依赖亮屏/解锁（比 WhatsApp UI 稳一个数量级）  
- Saily 已明确走**本机短信栈** → 网关可直接发  
- 入站用 `SMS_RECEIVED` / 网关 webhook，比通知监听可靠  

WhatsApp 仍走本机 UI（后期），与 SMS 解耦。

---

## 2.1 服务器部署与断线（简单稳定，不过度设计）

### 分工（不变）

| 位置 | 职责 |
|---|---|
| **服务器** | 读 Notion、日排程、到点发指令、回写 Notion、接外部 reply webhook |
| **手机** | Gateway 发/收短信；尽量 **主动连服务器**（不依赖服务器打进手机内网） |

### 断线时会发生什么（预期行为）

| 场景 | 结果 |
|---|---|
| 服务器 → 手机 发指令失败 | 该次发送失败；有限次重试后 Task=`Failed` + 中文 Notes；**不**长时间卡在 In Progress |
| 手机 → 服务器 入站上报失败 | 短信仍在手机收件箱；Notion 暂时无 Inbound；**恢复网络后由 Gateway 补推**（若 Gateway 支持）或人工对账 |
| 仅 Notion 可达、手机不可达 | 仍可拉当天任务并排程；到点发不出则按上表失败处理 |

原则：**断线不等于丢短信**；等于自动化暂停/部分失败，恢复后继续，失败任务可人工改期。

### 稳定措施（只做这几条）

1. **连通方式**  
   - 生产：Gateway **Cloud / 公网 HTTPS**（或等价：手机能访问的服务器域名）  
   - 不用「服务器直连手机局域网 IP」作为生产依赖  
   - USB/`adb` 仅开发调试  

2. **出站失败**  
   - 发送失败：有限重试（例如 3 次、指数退避）  
   - 仍失败：Task=`Failed`，`Ended At`，Notes 写清「网关不可达/发送失败」  
   - 下次日拉取不会自动重发同一条（除非人工改回 Pending 并改 Scheduled At）  

3. **入站**  
   - Webhook 指向服务器公网 URL  
   - 优先开启 Gateway 自带的失败重试/补推（有则开，无则接受短时缺口 + 心跳告警）  
   - 不另建复杂消息队列  

4. **心跳（最小）**  
   - 服务器记录「最近一次成功联系手机的时间」（发成功或收到入站均可）  
   - 超过阈值（例如 30–60 分钟）无联系 → 打一条日志/可选通知；人工查 Wi‑Fi、流量、Gateway 是否被杀  

5. **手机侧**  
   - 插电、关电池优化、Gateway 开机自启、前台服务  

**明确不做：** 多机热备、分布式队列、手机端完整编排、自动改 Scheduled At 滚到次日。

### 验收（并入 Gate F）

- 断网约 10 分钟再恢复：恢复后能继续发/收至少各 1 条  
- 断网期间到点任务：最终 Failed 或恢复后补发策略与文档一致（默认：有限重试后 Failed）  
- 心跳超时有日志（通知可选）

---

## 3. 稳定性完整清单（你担心的点）

### 3.1 一直充电是否可行？

**可行，且是推荐运行方式。**

| 点 | 做法 |
|---|---|
| 供电 | USB 常充或原装充电器；避免虚接 |
| 电池健康 | 可开「自适应充电」；专用机可接受长期插电 |
| 散热 | 勿闷在被子/厚壳里；温度过高会降频甚至断网 |
| 屏幕 | 可息屏；SMS **不需要**常亮（WA 阶段再另说） |

### 3.2 网络

| 风险 | 对策 |
|---|---|
| 蜂窝不稳定 | Saily 线路实测；编排层发送失败重试（指数退避） |
| Wi‑Fi 断了导致 webhook 达不到电脑 | 手机 Gateway 用 **Cloud 模式** 或 **电脑与手机同网 + 心跳**；断线告警 |
| 仅 USB 调试 | 开发期 OK；7×24 应用 **Wi‑Fi ADB 或局域网 HTTP**，别只靠一根线 |

### 3.3 软件常驻 / 唤醒

| 风险 | 对策（Pixel） |
|---|---|
| App 被杀 | 关电池优化；Gateway 用前台通知服务 |
| Doze | **插电**时限制弱很多；再加定期心跳 |
| 重启后不自启 | 开「开机启动」；编排层发现离线则告警 |
| 系统更新打断 | 关掉自动更新日期或人工窗口更新 |

### 3.4 「收到通知能否监听到」——SMS vs WA

| 通道 | 监听方式 | 可靠性 |
|---|---|---|
| SMS | 系统广播 / Gateway 收件箱监听 | **高**（不依赖通知栏文案） |
| WhatsApp（后期） | 通知监听为主 | 中：依赖通知权限与预览开关 |

SMS 阶段**不以通知栏为唯一来源**。

### 3.5 可观测性（7×24 必备）

- 每 N 分钟心跳：Gateway online + 上次入站/出站时间  
- 连续失败 / 心跳超时 → 推送（Telegram/邮件，可选）  
- ConversationDB 与本地日志双写，便于对账  

---

## 4. 分步验证计划（先功能，后稳定）

### Gate A — 本机短信人工基线（你操作，5 分钟）

1. 设置 → 网络 → SIM：默认**通话/短信** = Saily  
2. 系统 Messages 用 Saily 号给自己另一部手机发 1 条、收回 1 条  
3. **通过标准：** 人工收发都成功  

### Gate B — USB 调试可见（阻塞开发）

当前本机 `adb devices` **为空**。请你操作：

1. 设置 → 关于手机 → 连续点「版本号」开开发者选项  
2. 开发者选项 → 打开 **USB 调试**  
3. USB 连接 Mac → 手机点「允许这台电脑调试」  
4. 告诉我后我再跑 `adb devices`

### Gate C — SMS 可编程发送

1. 安装开源 SMS Gateway（或等价）到 Pixel  
2. 授权短信；指定 **Saily subscription**  
3. 从电脑 `POST` 发一条测试短信  
4. **通过标准：** 对方手机收到，且能从 API 查到 sent  

### Gate D — SMS 可编程接收

1. 配置入站 webhook → 本机 orchestrator  
2. 用另一部手机给 Saily 号发短信  
3. **通过标准：** orchestrator 日志出现 from/body/time  

### Gate E — Notion 闭环

日调度流程（详见 `docs/TEST_NOTION_SMS.md`）：

1. 按 **America/New_York** 拉取当天 `Channel=SMS` + `Pending` + `Scheduled At=当天`
2. Priority P0→P1→P2，在 09–12 / 14–18 随机排程；到点前复核仍为 Pending
3. 收件人：Contact→Key Person→Phone；正文：Task 唯一 Conversation→Content
4. 发送成功/失败：更新 Task `Status` + `Ended At`；更新既有 Conversation Sent/Failed
5. 按对方手机号缓存最近 Task；入站写 Conversation 并调外部 reply webhook（URL 可配）  
   - **Reply 回写完整契约与缓存字段见 [PLAN_REPLY_INGEST.md](./PLAN_REPLY_INGEST.md)**（`POST /api/replies`）
6. **通过标准：** `pytest` 全绿；真机至少一条出站 + 入站匹配成功

### Gate F — 稳定性最小集（SMS）

1. 手机插电、息屏、关电池优化  
2. 断网约 10 分钟再恢复（看重连与补推/继续发送）  
3. 强制停止 Gateway 再拉起（看自愈）  
4. 心跳超时有日志（见 §2.1）  
5. **通过标准：** 恢复后能再跑通收发；断线任务按 §2.1 失败策略处理；无大面积 Notion 错乱  

### Gate F2 — 无 USB + 服务器编排（产品验收）

1. **编排跑在服务器**；手机仅 Gateway；**拔掉 USB**  
2. 手机经公网/Cloud 连服务器 → 完成「当天 SMS 任务发送 + 入站回写」  
3. 手机总开关 **OFF** → 不再自动外发（入站默认可记）  
4. **通过标准：** 不插电脑也能跑；断线行为符合 §2.1  

### Gate G — WhatsApp（本机 UI，见 [PLAN_WHATSAPP.md](./PLAN_WHATSAPP.md)）

1. Notion：`Channel=WhatsApp`，流程与 SMS 相同（日调度 / Contact→Phone / Conversation Content）  
2. 手机 **WA Companion**：轮询服务器 job → `wa.me` + 无障碍发送；NotificationListener 入站上报  
3. **不做** Web/Baileys/扫码桥  
4. **通过标准：** Gate G1–G4（发送、通知入站、Notion 闭环、回复匹配）  

---

## 5. SMS 技术选型（本期落地）

| 组件 | 选择 | 原因 |
|---|---|---|
| 手机端 | [SMS Gateway for Android](https://github.com/capcom6/android-sms-gateway)（或 httpSMS） | 成熟 REST + webhook；走系统短信 |
| 编排 | 本仓库 `channel-orchestrator`（Python） | 可控、易接 Notion；先不绑死 n8n |
| Notion | 官方 API | 写 Conversation / 更新 Task |
| 部署 | **服务器常开**跑 orchestrator；手机 Gateway 公网连服务器 | 简单、可 7×24；开发期可用 Mac + USB |


备选：Tasker 收发 + HTTP —— 功能够，但运维与权限不如专用 Gateway 清晰。

---

## 6. 开发顺序（从现在开始）

1. ~~Notion 建表~~ → **跳过**  
2. 文档对齐（本文件）  
3. **你**：打开 USB 调试，让 `adb devices` 出现 Pixel  
4. **我**：装 Gateway APK、脚手架 orchestrator、打通 Gate C/D  
5. **一起**：Gate E Notion 闭环  
6. Gate F 稳定性  
7. 再开 WhatsApp  

---

## 7. 7×24 运行画像（SMS 目标态）

- Pixel 固定插电、息屏、信号良好；Gateway 前台常驻 + 开机自启  
- **orchestrator 部署在服务器**；手机经 Cloud/HTTPS 连服务器（见 §2.1）  
- 最小心跳 + 出站有限重试；不做复杂队列  
- WhatsApp **未**纳入前，不要求屏幕常亮  

这套对 SMS 是现实可落地的；WhatsApp 7×24 要额外「可解锁执行环境」，复杂度更高，故严格放后。
