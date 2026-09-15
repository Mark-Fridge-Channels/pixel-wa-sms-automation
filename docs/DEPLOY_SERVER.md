# 服务器部署（Caddy + orch.fcconnect.co）

目标：`Email + WhatsApp + SMS` 均由服务器上的 `channel-orchestrator` 编排；手机只负责 SMS Gateway / WA Companion 执行层。

## 域名 / Caddy

GoDaddy DNS：

- 主机：`orch`
- 类型：`A`
- 值：服务器公网 IP

`/etc/caddy/Caddyfile`：

```caddy
mail.fcconnect.co {
  reverse_proxy 127.0.0.1:3737
}

orch.fcconnect.co {
  encode gzip
  reverse_proxy 127.0.0.1:8787
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 代码目录

```bash
sudo mkdir -p /data
sudo chown "$USER":"$USER" /data
cd /data
git clone <本仓库 URL> pixel-wa-sms-automation
cd pixel-wa-sms-automation/channel-orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env（见下）
```

## SMS 怎么跑（重要）

手机上的 **SMS Gateway App 本身就是服务**（Local / Cloud 开关）。

| 模式 | 服务器如何发短信 | 入站 webhook |
|------|------------------|--------------|
| **Cloud（推荐 7×24）** | `SMS_GATEWAY_URL=https://api.sms-gate.app` + Cloud 账号 | 在 App/Dashboard 登记 `https://orch.fcconnect.co/webhook/sms` |
| Local + USB/同网 | `http://手机局域网:8080` | 手机要能访问公网 webhook（或内网穿透） |
| Local + Tailscale | `http://100.x.x.x:8080` | webhook 仍用 `https://orch.fcconnect.co/webhook/sms` |

**生产推荐 Cloud**：服务器不依赖家里 Wi‑Fi/USB；手机开着 Cloud Server 即可收发。

WhatsApp：Companion 把 Server URL 设为 `https://orch.fcconnect.co`，填 `WA_API_TOKEN`。

## `.env`（服务器）

```bash
WEBHOOK_BASE=https://orch.fcconnect.co
WA_API_TOKEN=<强随机>
REPLY_WEBHOOK_URL=https://followup-portal.fridgechannels.com/api/replies
REPLY_WEBHOOK_TOKEN=<生产 token>
NOTION_TOKEN=...
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
GMAIL_USER=mark@fridgechannels.com

# SMS Cloud 示例（以 App 显示为准）
SMS_GATEWAY_URL=https://api.sms-gate.app
SMS_GATEWAY_USER=...
SMS_GATEWAY_PASSWORD=...
SMS_SIM_NUMBER=2
SAILY_PHONE_E164=+18207863604
```

## systemd（两进程）

### 1) API / webhook / WA jobs

`/etc/systemd/system/channel-orch-serve.service`：

```ini
[Unit]
Description=Channel Orchestrator HTTP (webhook + WA jobs)
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/data/pixel-wa-sms-automation/channel-orchestrator
EnvironmentFile=/data/pixel-wa-sms-automation/channel-orchestrator/.env
ExecStart=/data/pixel-wa-sms-automation/channel-orchestrator/.venv/bin/python -m channel_orchestrator.cli serve --host 127.0.0.1 --port 8787
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### 2) 调度（Email/SMS/WA 出站 + Gmail poll）

`/etc/systemd/system/channel-orch-scheduler.service`：

```ini
[Unit]
Description=Channel Orchestrator Scheduler
After=network.target channel-orch-serve.service

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/data/pixel-wa-sms-automation/channel-orchestrator
EnvironmentFile=/data/pixel-wa-sms-automation/channel-orchestrator/.env
ExecStart=/data/pixel-wa-sms-automation/channel-orchestrator/.venv/bin/python -m channel_orchestrator.cli run-scheduler --channel all
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now channel-orch-serve channel-orch-scheduler
curl -sS https://orch.fcconnect.co/health
```

## 验收清单

1. `https://orch.fcconnect.co/health` → ok  
2. Companion heartbeat 出现在 health  
3. SMS Gateway Cloud ON，webhook 指向 `/webhook/sms`  
4. `run-once --channel Email|SMS|WhatsApp` 各测一条（测试联系人）  
5. 三渠道回复能匹配并 `POST /api/replies`
