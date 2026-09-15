# Email（Gmail 服务端）方案

> 更新：2026-09-14  
> 执行面：**SMS / WhatsApp = Android**；**Email = 编排服务器 Gmail API**  
> Portal 契约：[Reply 回写文档](https://app.notion.com/p/3db9166fd9fd80629ff1c3befe56c660)

## 要点

- 系统 `threadId` = Conversation.`Thread ID`（如 `THR-…-Email`），只拷贝。
- Gmail 线程 = Conversation.`Extended Parameters` JSON 中的 `gmailThreadId` / `gmailMessageId`。
- 新开线程：Extended Parameters 空 → `messages.send` 新线程；成功后回写 Gmail ids。
- 跟帖：Extended Parameters 有 `gmailThreadId` → 同线程回复。
- 入站：轮询 `users.history.list` → `POST /api/replies`（`channel=Email`，`messageId=""`，ext 带 Gmail ids）。

## 前置（mark@fridgechannels.com）

### A. Google Cloud Console 逐步（带 URL）

登录账号建议：有 **fridgechannels.com Workspace 管理权限** 的账号（或能创建 GCP 项目的人）。目标邮箱是 `mark@fridgechannels.com`。

#### 1) 打开 Cloud Console / 选项目

- 总入口：https://console.cloud.google.com/  
- 项目选择器：https://console.cloud.google.com/projectselector2  
- 新建项目：https://console.cloud.google.com/projectcreate  
  - 例如项目名：`fridge-pixel-email`

记下项目 ID；后续链接都在该项目下操作。

#### 2) 启用 Gmail API

- 直接打开（会提示选项目）：  
  https://console.cloud.google.com/apis/library/gmail.googleapis.com  
- 或 API 库搜索页：  
  https://console.cloud.google.com/apis/library?q=gmail  
- 点 **Enable / 启用**

启用后可在这里确认：  
https://console.cloud.google.com/apis/dashboard

#### 3) 配置 OAuth 同意屏幕（Consent screen）

- 入口：https://console.cloud.google.com/apis/credentials/consent  
- **User type**：选 **Internal**（仅本 Workspace 组织内，不用公开审核）  
- App name：例如 `Pixel Email Orchestrator`  
- User support email / Developer contact：填你的公司邮箱  
- 保存并继续；Scopes 可先跳过（授权 URL 会带 scope），或手动加：
  - `https://www.googleapis.com/auth/gmail.send`
  - `https://www.googleapis.com/auth/gmail.modify`

官方 scope 说明：  
https://developers.google.com/gmail/api/auth/scopes

#### 4) 创建 OAuth Desktop 客户端

- 凭据页：https://console.cloud.google.com/apis/credentials  
- **+ CREATE CREDENTIALS** → **OAuth client ID**  
- Application type：**Desktop app**  
- Name：例如 `pixel-email-desktop`  
- 创建后复制：
  - **Client ID** → `.env` 的 `GMAIL_CLIENT_ID`
  - **Client secret** → `.env` 的 `GMAIL_CLIENT_SECRET`

也可下载 JSON，但本项目用 `.env` 三项即可。

#### 5)（若授权被拦）Workspace 放行第三方应用

用管理员打开 Admin Console：  
https://admin.google.com/

常见路径（界面文案可能略有出入）：

- Security → Access and data control → API controls  
  或直接搜：https://admin.google.com/ac/owl  
- 把刚建的 OAuth 客户端标为受信任，或允许用户授权内部应用  

没有管理员权限时，需要 IT 帮你放行该 Client ID。

#### 6) 本机授权拿 refresh token

在仓库里：

```bash
cd channel-orchestrator
# 先写入 GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_USER=mark@fridgechannels.com
python -m channel_orchestrator.cli gmail-auth
```

浏览器会打开 Google 登录；**必须登录** `mark@fridgechannels.com`。  
同意后终端打印 `GMAIL_REFRESH_TOKEN=...`，写回 `.env`。

本地回调地址（脚本已写死，无需在 GCP 额外配置 Desktop 类型）：  
`http://127.0.0.1:8765/oauth2callback`

#### 7) 验证

```bash
python -m channel_orchestrator.cli gmail-poll   # 能跑通且不报 token 错误即可
```

### B. `.env` 最终形态

```
GMAIL_CLIENT_ID=.....apps.googleusercontent.com
GMAIL_CLIENT_SECRET=.....
GMAIL_REFRESH_TOKEN=.....
GMAIL_USER=mark@fridgechannels.com
GMAIL_POLL_SECONDS=30
```

**不要**把 Client Secret / Refresh Token 提交到 git。

## 配置

```
GMAIL_CLIENT_ID=
GMAIL_CLIENT_SECRET=
GMAIL_REFRESH_TOKEN=
GMAIL_USER=mark@fridgechannels.com
GMAIL_POLL_SECONDS=30
```

## 验收

- E1 新邮件发出且 Conversation Extended Parameters 有 `gmailThreadId`
- E2 带 `gmailThreadId` 的 Task 跟帖不新开线程
- E3 对方回复 → replies 成功，Portal Inbound Needs Reply
- E4 SMS/WA 路径不受影响
