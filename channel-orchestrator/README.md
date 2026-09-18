# Channel Orchestrator (SMS / WhatsApp / Email)

服务器侧编排：Notion **到期扫描**（Pending + Scheduled At≤now）、SMS Gateway、WhatsApp job 队列、**Gmail API 发信/听信**、入站 webhook、`/monitor` 看板。

## 配置

```bash
cp .env.example .env
pip install -r requirements.txt
```

关键项：`NOTION_TOKEN`、`SMS_GATEWAY_*`、`WA_API_TOKEN`（可选）、`REPLY_WEBHOOK_URL`、Email 时 `GMAIL_*`。

调度：`SCHEDULER_SCAN_SECONDS`（查 Notion 周期）、`SEND_GAP_SECONDS`（SMS/WA 串行间隔；Email 并行）。看板可热改间隔。`WA_DAILY_SEND_LIMIT=0` 表示不限额。

## 命令

```bash
python -m channel_orchestrator.cli ping
python -m channel_orchestrator.cli serve --port 8787

# 立刻扫描 Notion 到期 Pending 并按队列执行（测试用）
python -m channel_orchestrator.cli run-once --channel all
python -m channel_orchestrator.cli run-once --channel all --force

# 常驻：扫描 → 入队 → SMS/WA 串行 + Email 并行；Gmail 入站独立轮询
python -m channel_orchestrator.cli run-scheduler --channel all

# （legacy）仍可生成 daily_plan JSON，出站主路径已不再依赖
python -m channel_orchestrator.cli plan-today --channel SMS

# Gmail 一次性授权（浏览器登录业务邮箱）
python -m channel_orchestrator.cli gmail-auth
python -m channel_orchestrator.cli gmail-poll

# Follow-up Client Domain 缓存（冷进线）
python -m channel_orchestrator.cli sync-client-domains
```

## 监控

浏览器打开 `https://orch.fcconnect.co/monitor`（或本机 `http://127.0.0.1:8787/monitor`）：

- 今日领取 / 执行 / 成功 / 失败
- SMS / WhatsApp / Email 在线健康（WhatsApp 会显示 `google=` / `vpn=` 若 Companion 已上报）
- 调整扫描间隔与 SMS/WA 发送间隔（需 `MONITOR_TOKEN` 或 `WA_API_TOKEN`）

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
