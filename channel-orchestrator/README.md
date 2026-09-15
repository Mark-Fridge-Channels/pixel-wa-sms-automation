# Channel Orchestrator (SMS / WhatsApp / Email)

服务器侧编排：Notion 日调度、SMS Gateway、WhatsApp job 队列、**Gmail API 发信/听信**、入站 webhook。

## 配置

```bash
cp .env.example .env
pip install -r requirements.txt
```

关键项：`NOTION_TOKEN`、`SMS_GATEWAY_*`、`WA_API_TOKEN`（可选）、`REPLY_WEBHOOK_URL`、Email 时 `GMAIL_*`。

## 命令

```bash
python -m channel_orchestrator.cli ping
python -m channel_orchestrator.cli serve --port 8787

python -m channel_orchestrator.cli plan-today --channel SMS
python -m channel_orchestrator.cli plan-today --channel WhatsApp
python -m channel_orchestrator.cli plan-today --channel Email
python -m channel_orchestrator.cli run-once --channel all
python -m channel_orchestrator.cli run-scheduler --channel all

# Gmail 一次性授权（浏览器登录业务邮箱）
python -m channel_orchestrator.cli gmail-auth
python -m channel_orchestrator.cli gmail-poll
```

## WhatsApp API（Companion 轮询）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/wa/jobs/next` | 领取下一条待发 |
| POST | `/wa/jobs/{id}/result` | `{"ok": true/false, "error": "..."}` |
| POST | `/webhook/whatsapp` | 入站通知上报 |
| POST | `/wa/heartbeat` | 心跳 |

Header（若配置了 token）：`Authorization: Bearer <WA_API_TOKEN>`

## 测试

```bash
pytest -q
```
