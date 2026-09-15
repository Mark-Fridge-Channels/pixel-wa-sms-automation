# 服务器 Docker 部署（orch + SMS Gate Private）

目标：`Email + WhatsApp + SMS` 全部经 `fcconnect.co` 域名运行。

| 子域 | 用途 | 反代到 |
|------|------|--------|
| `orch.fcconnect.co` | channel-orchestrator（webhook / WA jobs / health） | `127.0.0.1:8787` |
| `smsgate.fcconnect.co` | **自建 SMS Gate Private Server**（手机 Cloud 连这里） | `127.0.0.1:3000` |
| `mail.fcconnect.co` | 已有 | `127.0.0.1:3737` |

> 可以。官方支持 Private Server；手机 App 填你的 HTTPS 域名即可，消息内容留在你自己的服务器（推送通知仍可走官方 FCM 通道）。

## 1. DNS（GoDaddy）

| 主机 | 类型 | 值 |
|------|------|-----|
| `orch` | A | 服务器公网 IP |
| `smsgate` | A | 同上 |

## 2. Caddy

把仓库里 `channel-orchestrator/deploy/Caddyfile.snippet` 合并进 `/etc/caddy/Caddyfile`，然后：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 3. 准备配置

```bash
cd /data/pixel-wa-sms-automation
git pull

cd channel-orchestrator
cp .env.example .env
# 编辑 .env：NOTION / GMAIL / REPLY / WA_API_TOKEN / SMS_GATEWAY_*

# 编辑 SMS Gate 私有云配置（private_token + DB 密码必须改）
nano deploy/smsgate/config.yml
# gateway.private_token  ← 手机里填同一个
# database.password      ← 与 .env 里 SMSGATE_DB_PASSWORD 一致
```

`.env` 关键项示例：

```bash
WEBHOOK_BASE=https://orch.fcconnect.co
WA_API_TOKEN=<强随机>

SMS_GATEWAY_MODE=private
SMS_GATEWAY_URL=https://smsgate.fcconnect.co
# 手机 Cloud Online 后 App 会显示 username/password，再填到下面两行：
SMS_GATEWAY_USER=
SMS_GATEWAY_PASSWORD=

REPLY_WEBHOOK_URL=https://followup-portal.fridgechannels.com/api/replies
REPLY_WEBHOOK_TOKEN=...
NOTION_TOKEN=...
GMAIL_*=...
```

## 4. 启动 Docker

```bash
cd /data/pixel-wa-sms-automation/channel-orchestrator
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=80 orch-serve smsgate
```

验收：

```bash
curl -sS http://127.0.0.1:8787/health
curl -sS https://orch.fcconnect.co/health
curl -sS https://smsgate.fcconnect.co/health
```

## 5. 手机（第 4 步）

### SMS Gateway App
1. Settings → Cloud Server  
2. API URL：`https://smsgate.fcconnect.co/api/mobile/v1`  
3. Private Token：与 `deploy/smsgate/config.yml` 里一致  
4. 打开 Cloud Server → Online  
5. 记下生成的 Username / Password → 写入服务器 `.env` 的 `SMS_GATEWAY_USER/PASSWORD`  
6. 注册入站 webhook（在服务器上）：

```bash
docker compose exec orch-serve \
  python -m channel_orchestrator.cli webhook-register \
  --url https://orch.fcconnect.co/webhook/sms
```

### WA Companion
- Server URL：`https://orch.fcconnect.co`  
- API token：与 `WA_API_TOKEN` 一致  
- 无障碍 + 通知权限保持开启  

改完 `.env` 后：

```bash
docker compose up -d --force-recreate orch-serve orch-scheduler
```

## 6. 常用命令

```bash
docker compose logs -f orch-scheduler
docker compose exec orch-serve python -m channel_orchestrator.cli ping
docker compose exec orch-serve python -m channel_orchestrator.cli run-once --channel Email --force
docker compose restart orch-serve orch-scheduler
```

## 架构关系

```
Notion / Portal / Gmail
        ↑
 channel-orchestrator (orch.fcconnect.co)
   ├─ SMS 出站 → smsgate.fcconnect.co → 推送到手机 Gateway App → 真发短信
   ├─ SMS 入站 ← 手机 webhook → orch /webhook/sms
   ├─ WA 出站  → wa_jobs ← Companion 轮询
   └─ Email    → Gmail API
```
