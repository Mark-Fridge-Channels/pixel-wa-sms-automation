# Notion SMS 闭环测试计划

对应实现：`channel-orchestrator/`（日调度 + 出站/入站回写）

时区：**America/New_York**  
工作窗：**09:00–12:00**、**14:00–18:00**

## 自动化单测（必过）

```bash
cd channel-orchestrator
pip install -r requirements.txt
pytest -q
```

覆盖：

| ID | 内容 |
|---|---|
| T1 过滤形状 | Channel/Status/Scheduled At 查询条件 |
| T2 窗口与间隔 | 无午休时段；排程间隔 ≥90s |
| T3 复核跳过 | Notion 非 Pending → skipped，不发送 |
| T5 缺正文 | resolve_error → Failed |
| T7 未匹配入站 | webhook matched=false |
| T8 同号覆盖 | 缓存保留最后一次成功 Task |
| T9 持久化 | plan 落盘 / due 扫描 |
| T10 时区日切 | 纽约日历日边界 |
| T11 成功路径（mock） | Completed + 缓存 pageId |

## 真机验收（T4 / T6）

前置：Gateway 在线、`adb forward`/`reverse` 可用、`.env` 含 `NOTION_TOKEN`。

1. 在 TaskDB 建一条当天 `Scheduled At`、`Channel=SMS`、`Pending`
2. Contact→Key Person 有有效 Phone；Task 关联唯一 Conversation，`Content` 非空
3. `python -m channel_orchestrator.cli plan-today` 可见该任务
4. 将 plan 中 `scheduled_at` 改成近几分钟（或等窗口），`run-once`
5. **T4 期望**：短信发出；Task=`Completed` + `Ended At`；Conversation=`Sent`；`data/last_outbound_by_phone.json` 有该号
6. 用该号回复一条短信；orchestrator `serve` 收到 webhook
7. **T6 期望**：新建 Inbound Conversation 挂正确 Task；若配置了 `REPLY_WEBHOOK_URL` 则收到事件（否则 log skip）

## 手工补充

- T5：故意清空 Content 或 Phone → Failed + 中文 Notes
- 重启：`run-scheduler` 杀进程再启，未执行 planned 项继续，completed 不重发
