# WhatsApp Companion — Gate 验收

## 前置

- WhatsApp 已用 Saily 号登录本机  
- 服务器 `python -m channel_orchestrator.cli serve` 可被手机访问（公网或同网）  
- Companion 已开无障碍、通知使用权、总开关 ON  

## G1 发送

1. 服务器手动入队或跑通 Notion WhatsApp Pending  
2. Companion 拉到 job 并打开 WhatsApp 发出  
3. `POST /wa/jobs/{id}/result` success；Notion Task Completed（若走完整链路）

## G2 入站通知

1. 测试号回复 WA  
2. 服务器 `/webhook/whatsapp` 或日志出现 from/body  
3. Notion 有 Inbound Conversation（若 token 已配）

## G3 / G4

见 [TEST_NOTION_SMS.md](./TEST_NOTION_SMS.md) 同等步骤，Channel 改为 WhatsApp；缓存文件为 `last_outbound_by_phone_whatsapp.json`。

## 限速

- `WA_MIN_SEND_INTERVAL_SECONDS` 默认 120  
- `WA_DAILY_SEND_LIMIT` 默认 30  
