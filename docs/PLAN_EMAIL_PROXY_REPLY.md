# Email：发给 A、由 B 回复（同公司）

> 更新：2026-09-15  
> **状态：已实现**（`email_util.classify_email_reply_relation` + `inbound`）

## 原则

1. **主匹配键 = `gmailThreadId`**（缓存 / Extended Parameters）  
2. From 只做身份标注，不是唯一准入条件  
3. **禁止**仅凭同域名匹配（防误挂）

## 匹配顺序

1. classify ≠ human → bounce / auto（已有）  
2. `get_ready_by_gmail_thread` 命中 → 挂原 Task，并标注：
   - From = 出站 A → 正常  
   - From = B 同公司域 → `proxyReply=true`  
   - From 不同域 → `unexpectedSender=true`（默认仍接受；可用 env 关掉）  
3. 无线程 → 仅当 From = 出站邮箱时 `get_ready_for_reply`  
4. 否则 unmatched  

## Portal extendedParameters

```json
{
  "gmailThreadId": "…",
  "gmailMessageId": "…",
  "outboundTo": "a@acme.com",
  "replyFrom": "b@acme.com",
  "replyMatch": "gmail_thread",
  "proxyReply": true,
  "sameOrgDomain": true,
  "unexpectedSender": false
}
```

## 配置

```
EMAIL_PUBLIC_DOMAINS=           # 空=内置公共域列表；公共域不做「同公司」放宽
EMAIL_ALLOW_CROSS_DOMAIN_THREAD_REPLY=true
```
