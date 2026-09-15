# WA Companion (Android)

本机 WhatsApp 执行器：轮询服务器 job → `wa.me` + 无障碍点发送；NotificationListener 上报入站。

## 构建

用 Android Studio 打开本目录，Sync Gradle 后 Run 到 Pixel。

或（本机已装 SDK / Gradle Wrapper 时）：

```bash
cd wa-companion
# 若无 wrapper：Android Studio 生成 gradle wrapper 后再
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## 配置

1. 打开 App：填服务器 `http://<host>:8787`、可选 API token（与 orchestrator `WA_API_TOKEN` 一致）  
2. 打开 **无障碍**、**通知使用权**  
3. 总开关 **ON** → 前台服务轮询  

## 与编排联调

```bash
# 服务器
cd channel-orchestrator
python3 -m channel_orchestrator.cli serve --port 8787
python3 -m channel_orchestrator.cli plan-today --channel WhatsApp
python3 -m channel_orchestrator.cli run-once --channel WhatsApp
```

手机与服务器需网络互通（开发期可用同 Wi‑Fi + 电脑局域网 IP）。
