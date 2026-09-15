# 手机端设置清单（SMS Gateway）

包名：`me.capcom.smsgateway`（已安装 v1.67.0 insecure，便于局域网明文 HTTP 调试）

## 你要在手机上完成的操作（总开关相关）

1. 打开 **SMS Gateway** App  
2. 授予全部弹出的权限：**短信、电话、通知**  
3. 设置 → 应用 → SMS Gateway → **电池** → **不限制**  
4. 在 App 内开启服务（这就是现阶段的 **自动化总开关**）：  
   - Local Server **或** Cloud Server **ON**  
   - 关 OFF = 停止自动发短信能力  
5. **SIM 选择**：选 **Saily**（当前机上有 `Saily #1` / `Saily #2`，选实际用于收发短信的那张）  
6. 设置 → 应用 → 默认应用 → **短信应用** → 选 **Messages（Google）**（不要用 Signal 当默认短信，除非你确认 Gateway 仍能收）  
7. 记下 App 显示的：  
   - Local 模式：局域网地址 + 用户名/密码  
   - 或 Cloud 模式：账号与 API 地址  

完成后把 **地址 + 用户名/密码**（可私发）给我，我接着跑 Gate C 发测短信。

## 与「不插 USB 也能跑」的关系

- Local：手机与编排在同一 Wi‑Fi；开关在 App 内  
- Cloud（推荐 7×24）：不依赖家里电脑在线即可收指令；开关仍在手机 App  

USB 仅用于安装/调试，不是运行依赖。
