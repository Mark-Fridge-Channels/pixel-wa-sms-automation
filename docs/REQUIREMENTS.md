# 需求文档

> 项目：Pixel WA/SMS 本机自动化  
> 状态：draft  
> 更新日期：2026-09-13

---

## 1. 背景与目标

需要一套跑在真机上的自动化系统，用于：

1. **按任务外联**：根据任务找到联系人，自动发送 WhatsApp 或短信。
2. **入站监听**：任何人回复时，记录「谁 / 何时 / 回复了什么」。
3. **同步落库**：将发送与接收事件同步到 Notion（可扩展到其他系统）。

核心原则：**在手机 WhatsApp App 与系统短信里完成动作**，模拟真人操作；**不**再使用扫码挂 Web / 多设备会话的第三方 WhatsApp 桥接（该类方案曾导致 Web 端无法使用）。

---

## 2. 运行环境（已明确）

| 项 | 值 |
|---|---|
| 设备 | Google Pixel 7 Pro |
| 系统 | Android 17 |
| 号码 | Saily 购买的 eSIM |
| 通话 | 使用手机**本机电话**功能 |
| 短信 | 使用手机**本机短信**功能（系统 Messages / 短信栈） |
| WhatsApp | 安装在本机的 WhatsApp App（用 Saily 号注册/绑定） |

### 2.1 关于 Saily

- Saily eSIM 在 Pixel 7 Pro 上作为本机蜂窝线路使用。
- **通话、短信走 Android 原生电话与短信能力**，因此：
  - 可用系统短信收发 API / 短信网关 / Tasker「收到短信」等本机手段做自动化与监听；
  - 不依赖 Saily 是否提供开发者 API（当前公开信息：Saily **无**公开 SMS Webhook/API）。

---

## 3. 功能需求

### 3.1 WhatsApp — 任务驱动发送

| ID | 需求 | 说明 |
|---|---|---|
| WA-S1 | 根据任务定位联系人 | 任务至少提供：手机号，或可解析出手机号的姓名/标识 |
| WA-S2 | 自动打开发送 | 在本机 WhatsApp App 内打开对应对话（或 `wa.me` 预填）并发送文本 |
| WA-S3 | 记录出站 | 记录：任务 ID、对方、发送时间、内容、结果（成功/失败） |
| WA-S4 | 限速与安全 | 支持间隔、每日上限、人工确认开关（防封控） |

**非目标（本期不做，除非另行批准）：**

- 通过 WhatsApp Web / Baileys / Evolution 扫码挂会话发送
- 稳定自动发送语音条（UI 模拟难度高，列为后续可选）

### 3.2 WhatsApp — 入站监听

| ID | 需求 | 说明 |
|---|---|---|
| WA-L1 | 捕获回复 | 有人发来消息时能感知 |
| WA-L2 | 结构化字段 | 至少：发送方标识（姓名/号码）、时间、正文 |
| WA-L3 | 关联任务 | 若该联系人存在未完成外联任务，回写「已回复」及摘要 |
| WA-L4 | 落库 | 写入 Notion（或中转服务后再写） |

**实现偏好：** 优先通知栏监听（NotificationListener）；必要时再辅以打开聊天读屏（无障碍）。

### 3.3 短信 — 任务驱动发送

| ID | 需求 | 说明 |
|---|---|---|
| SMS-S1 | 从本机 Saily 线路发出 | 使用系统短信，发件线路为 Saily eSIM |
| SMS-S2 | 按任务找人发送 | 同 WA：任务 → 手机号 → 发送纯文本 |
| SMS-S3 | 记录出站 | 同 WA-S3 |

**约束：** Saily 号码侧通常不支持 MMS；只发标准 SMS 纯文本。

### 3.4 短信 — 入站监听

| ID | 需求 | 说明 |
|---|---|---|
| SMS-L1 | 捕获入站 SMS | 系统收到短信即触发 |
| SMS-L2 | 结构化字段 | 发送方号码、时间、正文 |
| SMS-L3 | 关联任务 + 落库 | 同 WA-L3 / WA-L4 |

### 3.5 任务与同步（使用既有 Notion，不新建表）

| ID | 需求 | 说明 |
|---|---|---|
| TASK-1 | 任务源 | [FC3.0-Follow-up-TaskDB](https://app.notion.com/p/ff50607a3fdd469eb5fc2592a33ff63b)（Channel=SMS/WhatsApp, Status=Pending） |
| TASK-2 | 回写 | [FC3.0-Follow-up-ConversationDB](https://app.notion.com/p/a7ded397b23f47f1a9113855fc3d10ce) |
| TASK-3 | 号码 | Follow-up Contact → Key Person.`Phone` |
| TASK-4 | 状态 | Task：Pending → In Progress → Completed/Failed；Conversation：Sent/Received/Failed |

---

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NF1 | 不依赖 WhatsApp 官方 Cloud API（本期）；本机 App UI 自动化 |
| NF2 | Pixel 7 Pro / Android 17 上验证通过 |
| NF3 | 手机需保持可执行 UI 自动化的条件（亮屏/解锁策略需在实现文档中定义） |
| NF4 | 自动化组件需关闭电池优化，避免后台被杀 |
| NF5 | 文档与配置不含密钥明文；Notion/Token 用本地安全存储 |

---

## 5. 明确不做（Out of Scope）

1. 再次接入「扫码关联设备」类 WhatsApp 第三方 Web 工具（高风险复现封禁）。
2. 笔记本 eSIM / 非 Pixel 设备适配（本期仅 Pixel 7 Pro）。
3. MMS、群发彩信、群聊大规模运营。
4. 完整 CRM 产品化 UI（先打通链路）。

---

## 6. 验收标准

1. 从 Notion 创建一条「发 WhatsApp」任务 → 手机自动发出 → Notion 有出站记录。
2. 对方回复 WhatsApp → 系统记录谁/何时/内容 → 任务状态变为已回复。
3. 从 Notion 创建一条「发 SMS」任务 → 经 Saily 本机线路发出 → 有出站记录。
4. 对方回短信 → 记录谁/何时/内容 → 可关联任务。
5. 全程**未**重新绑定 WhatsApp Web 类第三方会话。

---

## 7. 风险与假设

| 风险 | 说明 | 缓解 |
|---|---|---|
| WhatsApp UI 改版 | 控件 ID 变化导致脚本失效 | 版本锁定说明 + 选择器可配置 |
| 行为风控 | 即便本机 UI 也可能触发限制 | 限速、白名单联系人、人工确认 |
| 屏幕锁定 | UI 自动化需解锁 | 测试机策略：信任设备/延长解锁/专用测试机 |
| Saily 短信策略 | 套餐、国际短信到达率 | 先小流量实测收发 |
| 通知不全 | 部分消息不弹通知 | 兜底：定期打开未读或辅助读库（若可行） |

**假设：**

- Pixel 7 Pro 已解锁，Saily eSIM 已安装并可在系统设置中作为通话/短信线路。
- WhatsApp 已用该 Saily 号登录本机 App。
- 用户接受「本机 UI 自动化」路线，而非官方 Cloud API。
