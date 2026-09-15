# Pixel WA/SMS 本机自动化

在 **Google Pixel 7 Pro（Android 17）** 上，基于手机本机 WhatsApp App 与系统电话/短信能力，实现：

- 按任务自动找人并发送 WhatsApp / SMS
- 监听入站回复（谁 / 何时 / 内容）
- 将事件同步到 Notion

号码身份使用 **Saily eSIM**，走手机**原生通话与短信**。

## 文档

| 文档 | 说明 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | **完整架构图 + 每天如何执行（三通道）** |
| [docs/PLAN.md](docs/PLAN.md) | 主方案（含服务器部署与断线 §2.1） |
| [docs/PLAN_WHATSAPP.md](docs/PLAN_WHATSAPP.md) | WhatsApp 本机 Companion 方案 |
| [docs/PLAN_EMAIL.md](docs/PLAN_EMAIL.md) | Email / Gmail 服务端方案 |
| [docs/PLAN_EMAIL_PROXY_REPLY.md](docs/PLAN_EMAIL_PROXY_REPLY.md) | Email 发给 A、B 代回（同公司）方案设计 |
| [docs/PLAN_REPLY_INGEST.md](docs/PLAN_REPLY_INGEST.md) | Portal `POST /api/replies` 契约 |
| [docs/TEST_NOTION_SMS.md](docs/TEST_NOTION_SMS.md) | SMS Notion 测试 |
| [docs/TEST_WHATSAPP.md](docs/TEST_WHATSAPP.md) | WhatsApp Gate 验收 |
| [channel-orchestrator/](channel-orchestrator/) | 服务器编排（Notion 日调度：SMS / WhatsApp / Email） |
| [wa-companion/](wa-companion/) | Android Companion（WA 发送/通知入站） |

## 架构摘要

- **服务器**：`channel-orchestrator` 读 Notion、排程；SMS 调 Gateway；WhatsApp 写 `wa_jobs` 供手机轮询  
- **手机**：SMS Gateway App + WA Companion（无障碍 + 通知监听）  
- **不做**：WhatsApp Web / Baileys / 扫码桥  

## Notion（已有，不新建）

- Task：[FC3.0-Follow-up-TaskDB](https://app.notion.com/p/ff50607a3fdd469eb5fc2592a33ff63b)
- Conversation：[FC3.0-Follow-up-ConversationDB](https://app.notion.com/p/a7ded397b23f47f1a9113855fc3d10ce)
- 号码：Contact → Key Person.`Phone`

## 快速命令

```bash
cd channel-orchestrator && pip install -r requirements.txt
python3 -m channel_orchestrator.cli serve --port 8787
python3 -m channel_orchestrator.cli plan-today --channel SMS
python3 -m channel_orchestrator.cli plan-today --channel WhatsApp
python3 -m channel_orchestrator.cli plan-today --channel Email
python3 -m channel_orchestrator.cli run-scheduler --channel all
python3 -m pytest -q
```
