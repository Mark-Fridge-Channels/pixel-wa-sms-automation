# WhatsApp 号码探测 API

通过 Companion 打开 `api.whatsapp.com/send?phone=…`，**不发送消息**，判断号码是否注册 WhatsApp。

Base URL 示例：`https://orch.fcconnect.co`

## 鉴权（必填）

所有 `/wa/probe*` 接口**必须**带：

```http
Authorization: Bearer <token>
```

| 环境变量 | 说明 |
|---|---|
| `WA_PROBE_API_TOKEN` | 探测专用 token（优先） |
| `WA_API_TOKEN` | 未设专用 token 时回退使用（与 Companion 共用） |

未配置任一 token → **HTTP 503**；token 错误或缺失 → **HTTP 401**。

```bash
export ORCH=https://orch.fcconnect.co
export TOKEN='你的_WA_PROBE_API_TOKEN_或_WA_API_TOKEN'
```

---

## 接口一览

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/wa/probe` | 提交探测（入队，不建 Notion Task） |
| `GET` | `/wa/probe/{id}` | 按探测 ID 查结果 |
| `GET` | `/wa/probe?phone=+E164` | 按手机号查最新结果 |

约束：无日上限；全局入队间隔默认 **≥ 120s**（`WA_PROBE_INTERVAL_SECONDS`）。

---

## `POST /wa/probe`

### Request

| Header | 必填 | 说明 |
|---|---|---|
| `Authorization` | 是 | `Bearer <token>` |
| `Content-Type` | 是 | `application/json` |

| Body 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `phone` | string | 是* | E.164 手机号，如 `+15551234567`、`+8613812345678` |
| `number` | string | 否 | `phone` 的别名 |

\* `phone` 与 `number` 至少填一个。

```json
{ "phone": "+15551234567" }
```

### Response `200`

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | bool | 是否接受请求 |
| `queued` | bool | 本次是否新入队 |
| `reused` | bool | 同号仍在 `pending` 时复用已有探测 |
| `retry_after_seconds` | int | 建议下次再入队的间隔（秒） |
| `digits` | string | 纯数字（仅新入队时可能有） |
| `probe` | object | 见下表「probe 对象」 |

### Response `400` / `401` / `429` / `503`

| HTTP | 场景 |
|---|---|
| 400 | 缺 phone / 号码非法 |
| 401 | 无 Authorization 或 token 错误 |
| 429 | 距上次探测入队不足间隔（`detail.retry_after_seconds`） |
| 503 | 服务端未配置探测 token |

429 示例：

```json
{
  "detail": {
    "ok": false,
    "error": "probe interval 120s; retry after 87s",
    "retry_after_seconds": 87,
    "last_probe": null
  }
}
```

---

## `GET /wa/probe/{id}`

### Request

| Header | 必填 |
|---|---|
| `Authorization: Bearer <token>` | 是 |

路径参数 `id` = 入队返回的 `probe.id`（与 `job_id` 相同）。

### Response `200`

```json
{
  "ok": true,
  "probe": { "...": "见 probe 对象" }
}
```

`404`：id 不存在。

---

## `GET /wa/probe?phone=+15551234567`

### Request

| Query | 类型 | 必填 | 说明 |
|---|---|---|---|
| `phone` | string | 是 | E.164 |

Header 同其它接口。

### Response `200`

同 `GET /wa/probe/{id}`。`404`：该号尚无探测记录。

---

## `probe` 对象字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 探测 ID（UUID） |
| `phone` | string | 规范化后的 E.164 |
| `status` | string | `pending` \| `yes` \| `no` \| `unknown` \| `error` |
| `has_whatsapp` | bool \| null | `true` 有号 / `false` 无号 / `null` 未判定 |
| `job_id` | string \| null | Companion 队列 job id（通常等于 `id`） |
| `detail` | string \| null | 细节，如 `queued` / `composer` / 错误文案 |
| `updated_at` | string | ISO-8601 UTC |

### `status` × `has_whatsapp`

| status | has_whatsapp | 含义 |
|---|---|---|
| `pending` | `null` | 已入队 / 手机执行中 |
| `yes` | `true` | 出现聊天输入框 → 有 WA |
| `no` | `false` | 「不在 WhatsApp」类提示 → 无号 |
| `unknown` | `null` | 超时等无法判定 |
| `error` | `null` | 打开失败等错误 |

---

## 调用示例

```bash
# 1) 提交
curl -sS -X POST "$ORCH/wa/probe" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"phone":"+15551234567"}'
# → 记下 probe.id

# 2) 轮询（约每 3–5s，通常 10–30s 内出结果）
curl -sS "$ORCH/wa/probe/<probe.id>" \
  -H "Authorization: Bearer $TOKEN"

# 3) 或按号查
curl -sS --get "$ORCH/wa/probe" \
  --data-urlencode "phone=+15551234567" \
  -H "Authorization: Bearer $TOKEN"
```

一轮完整脚本：

```bash
PHONE='+15551234567'
RESP=$(curl -sS -X POST "$ORCH/wa/probe" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"phone\":\"$PHONE\"}")
echo "$RESP" | python3 -m json.tool
ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['probe']['id'])")

for i in $(seq 1 20); do
  R=$(curl -sS "$ORCH/wa/probe/$ID" -H "Authorization: Bearer $TOKEN")
  ST=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['probe']['status'])")
  echo "[$i] status=$ST"
  [ "$ST" != "pending" ] && echo "$R" | python3 -m json.tool && break
  sleep 3
done
```

---

## 如何测试

### A. 单元（无需手机）

```bash
cd channel-orchestrator
python3 -m pytest tests/test_wa_probe.py -q
```

### B. 鉴权冒烟（需已部署 orch + 已配 token）

```bash
# 应 401
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$ORCH/wa/probe" \
  -H "Content-Type: application/json" -d '{"phone":"+15551234567"}'

# 应 200 或 429
curl -sS -X POST "$ORCH/wa/probe" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"phone":"+15551234567"}'
```

### C. 端到端（需 Pixel + Companion ≥ 0.4.2）

1. Companion：Server URL = orch 域名；API token = **`WA_API_TOKEN`**（手机轮询用这个，可与探测专用 token 不同）
2. Automation ON；WhatsApp 无障碍已开；能 `GET /wa/heartbeat`
3. 用已知**有 WA** 的号码测 → 期望 `status=yes`，`has_whatsapp=true`
4. 用明显未注册号测 → 期望 `status=no`，`has_whatsapp=false`
5. 间隔内连发第二个不同号 → 期望 **429**
6. 同号在 `pending` 时再 POST → `reused=true`，不重复入队

### D. 服务器侧确认

```bash
# 队列里应出现 job_type=probe
docker compose exec orch-serve \
  python -c "from channel_orchestrator.wa_jobs import get_wa_queue; import json; print(json.dumps(get_wa_queue().list_jobs()[-3:], indent=2))"
```

手机需 Companion ≥ **0.4.2**，Automation ON，无障碍已开。
