from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import settings
from .exec_log import load_scan_state, read_recent_events, today_stats
from .gateway import SmsGatewayClient
from .heartbeat import heartbeat_stale, read_heartbeat, touch_heartbeat
from .inbound import handle_inbound_sms, handle_inbound_whatsapp
from .inbound_dedupe import describe_skip, seen_or_mark
from .outbound import finalize_whatsapp_job
from .outbound_queue import get_outbound_queue
from .runtime_settings import load_runtime_settings, save_runtime_settings
from .wa_jobs import get_wa_queue

app = FastAPI(title="Channel Orchestrator")

DATA_DIR = settings.resolved_data_dir()
INBOUND_LOG = DATA_DIR / "inbound.jsonl"
INBOUND: list[dict[str, Any]] = []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_log(event: dict[str, Any]) -> None:
    with INBOUND_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _check_wa_token(authorization: str | None) -> None:
    if not settings.wa_api_token:
        return
    expected = f"Bearer {settings.wa_api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def _monitor_token() -> str:
    return (settings.monitor_token or settings.wa_api_token or "").strip()


def _check_monitor_token(authorization: str | None) -> None:
    token = _monitor_token()
    if not token:
        return
    expected = f"Bearer {token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def normalize_inbound(body: dict[str, Any]) -> dict[str, Any]:
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
    return {
        "event": body.get("event") or "sms:received",
        "sender": payload.get("sender")
        or payload.get("phoneNumber")
        or payload.get("from")
        or payload.get("title"),
        "recipient": payload.get("recipient"),
        "body": payload.get("message") or payload.get("body") or payload.get("text"),
        "message_id": payload.get("messageId") or payload.get("id"),
        "sim_number": payload.get("simNumber"),
        "received_at": payload.get("receivedAt") or payload.get("received_at"),
        "device_id": body.get("deviceId"),
        "webhook_id": body.get("webhookId") or body.get("id"),
    }


def channel_health() -> dict[str, Any]:
    phone_hb = read_heartbeat().get("phone") or {}
    phone_online = not heartbeat_stale("phone")

    sms: dict[str, Any] = {"channel": "SMS", "online": False, "detail": ""}
    try:
        ping = SmsGatewayClient().ping()
        sms["gateway_ok"] = bool(ping.get("ok"))
        sms["detail"] = ping.get("error") or ping.get("host") or ""
        sms["online"] = bool(ping.get("ok")) and phone_online
        sms["phone_online"] = phone_online
    except Exception as e:  # noqa: BLE001
        sms["gateway_ok"] = False
        sms["online"] = False
        sms["detail"] = str(e)

    wa_jobs = get_wa_queue().list_jobs()
    pending_wa = sum(1 for j in wa_jobs if j.get("status") in {"queued", "leased"})
    wa = {
        "channel": "WHATSAPP",
        "online": phone_online,
        "phone_online": phone_online,
        "pending_jobs": pending_wa,
        "detail": phone_hb.get("last_seen") or "no heartbeat",
    }

    email: dict[str, Any] = {"channel": "EMAIL", "online": False, "detail": ""}
    if not settings.gmail_refresh_token:
        email["detail"] = "GMAIL_REFRESH_TOKEN missing"
    else:
        try:
            from .gmail_client import GmailClient

            profile = GmailClient().get_profile()
            email["online"] = True
            email["detail"] = profile.get("emailAddress") or settings.gmail_user or "ok"
        except Exception as e:  # noqa: BLE001
            email["online"] = False
            email["detail"] = str(e)[:200]

    return {"SMS": sms, "WHATSAPP": wa, "EMAIL": email, "checked_at": _now_iso()}


@app.get("/health")
def health() -> dict[str, Any]:
    hb = read_heartbeat()
    return {
        "ok": True,
        "time": _now_iso(),
        "inbound_count": len(INBOUND),
        "log": str(INBOUND_LOG),
        "saily": settings.saily_phone_e164 or None,
        "heartbeat": hb,
        "phone_stale": heartbeat_stale("phone"),
        "channels": channel_health(),
    }


@app.get("/inbound")
def list_inbound() -> dict[str, Any]:
    return {"count": len(INBOUND), "items": INBOUND[-50:]}


@app.post("/webhook/sms")
async def sms_webhook(request: Request) -> JSONResponse:
    raw = await request.json()
    normalized = normalize_inbound(raw if isinstance(raw, dict) else {})
    event = {
        "id": str(uuid.uuid4()),
        "channel": "SMS",
        "received_at_host": _now_iso(),
        "normalized": normalized,
        "raw": raw,
    }
    INBOUND.append(event)
    _append_log(event)
    # SMS Gate retries the same delivery if Notion/Portal handling is slow.
    if seen_or_mark(
        "SMS",
        normalized.get("message_id"),
        body=str(normalized.get("body") or ""),
        sender=str(normalized.get("sender") or ""),
    ):
        touch_heartbeat("phone")
        return JSONResponse(describe_skip("SMS", normalized.get("message_id")))
    result = handle_inbound_sms(normalized)
    touch_heartbeat("phone")
    return JSONResponse(
        {
            "ok": True,
            "id": event["id"],
            "matched": result.get("matched"),
            "task_page_id": result.get("task_page_id"),
        }
    )


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _check_wa_token(authorization)
    raw = await request.json()
    normalized = normalize_inbound(raw if isinstance(raw, dict) else {})
    event = {
        "id": str(uuid.uuid4()),
        "channel": "WHATSAPP",
        "received_at_host": _now_iso(),
        "normalized": normalized,
        "raw": raw,
    }
    INBOUND.append(event)
    _append_log(event)
    if seen_or_mark(
        "WHATSAPP",
        normalized.get("message_id"),
        body=str(normalized.get("body") or ""),
        sender=str(normalized.get("sender") or ""),
    ):
        touch_heartbeat("phone")
        return JSONResponse(describe_skip("WHATSAPP", normalized.get("message_id")))
    result = handle_inbound_whatsapp(normalized)
    touch_heartbeat("phone")
    return JSONResponse(
        {
            "ok": True,
            "id": event["id"],
            "matched": result.get("matched"),
            "task_page_id": result.get("task_page_id"),
        }
    )


@app.get("/wa/jobs/next")
def wa_job_next(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_wa_token(authorization)
    touch_heartbeat("phone")
    job = get_wa_queue().next_job()
    if not job:
        return JSONResponse({"ok": True, "job": None})
    # companion needs phone digits + text
    return JSONResponse(
        {
            "ok": True,
            "job": {
                "id": job["id"],
                "phone": job.get("phone"),
                "text": job.get("text"),
                "task_id": job.get("task_id"),
            },
        }
    )


@app.post("/wa/jobs/{job_id}/result")
async def wa_job_result(
    job_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _check_wa_token(authorization)
    body = await request.json()
    ok = bool(body.get("ok") or body.get("success"))
    error = body.get("error") or body.get("reason")
    queue = get_wa_queue()
    job = queue.complete(job_id, ok=ok, error=error, detail=body if isinstance(body, dict) else {})
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    result = finalize_whatsapp_job(job, ok=ok, error=error)
    touch_heartbeat("phone")
    return JSONResponse({"ok": True, "job_id": job_id, "finalize": result})


@app.get("/wa/jobs")
def wa_jobs_list(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_wa_token(authorization)
    return JSONResponse({"ok": True, "jobs": get_wa_queue().list_jobs()})


@app.post("/wa/heartbeat")
def wa_heartbeat(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_wa_token(authorization)
    touch_heartbeat("phone")
    return JSONResponse({"ok": True, "time": _now_iso()})


@app.get("/api/monitor/summary")
def monitor_summary() -> dict[str, Any]:
    return {
        "ok": True,
        "time": _now_iso(),
        "stats": today_stats(),
        "settings": load_runtime_settings(),
        "scan_state": load_scan_state(),
        "queue": get_outbound_queue().depth(),
        "channels": channel_health(),
        "recent": read_recent_events(40),
    }


@app.post("/api/monitor/settings")
async def monitor_settings(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _check_monitor_token(authorization)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected json object")
    updates = {}
    if "scan_interval_seconds" in body and body["scan_interval_seconds"] is not None:
        updates["scan_interval_seconds"] = body["scan_interval_seconds"]
    if "send_gap_seconds" in body and body["send_gap_seconds"] is not None:
        updates["send_gap_seconds"] = body["send_gap_seconds"]
    if not updates:
        raise HTTPException(status_code=400, detail="no settings provided")
    try:
        updated = save_runtime_settings(**updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JSONResponse({"ok": True, "settings": updated})


MONITOR_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Channel Orchestrator Monitor</title>
  <style>
    :root {
      --bg: #0f1419;
      --panel: #1a222c;
      --text: #e7ecf1;
      --muted: #8b9aab;
      --ok: #3dba7a;
      --bad: #e25c5c;
      --line: #2a3542;
      --accent: #5b9fd4;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background: radial-gradient(1200px 600px at 10% -10%, #1c2a3a, var(--bg));
      color: var(--text);
      min-height: 100vh;
    }
    main { max-width: 1100px; margin: 0 auto; padding: 28px 20px 60px; }
    h1 { font-size: 1.6rem; margin: 0 0 6px; letter-spacing: 0.02em; }
    .sub { color: var(--muted); margin-bottom: 22px; }
    .grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
    .card {
      background: color-mix(in srgb, var(--panel) 92%, black);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 14px 16px;
    }
    .label { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em; }
    .value { font-size: 1.6rem; margin-top: 6px; font-weight: 600; }
    .health { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin: 18px 0; }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }
    .dot.on { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 25%, transparent); }
    .dot.off { background: var(--bad); box-shadow: 0 0 0 3px color-mix(in srgb, var(--bad) 25%, transparent); }
    .detail { color: var(--muted); font-size: 0.85rem; margin-top: 8px; word-break: break-all; }
    form.settings { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; margin: 8px 0 20px; }
    label { display: flex; flex-direction: column; gap: 4px; font-size: 0.85rem; color: var(--muted); }
    input {
      background: #10161d; border: 1px solid var(--line); color: var(--text);
      border-radius: 8px; padding: 8px 10px; min-width: 140px;
    }
    button {
      background: var(--accent); color: #061018; border: 0; border-radius: 8px;
      padding: 9px 14px; font-weight: 650; cursor: pointer;
    }
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
    th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
    th { color: var(--muted); font-weight: 500; }
    .msg { min-height: 1.2em; color: var(--muted); margin: 6px 0 14px; }
  </style>
</head>
<body>
<main>
  <h1>Outbound Monitor</h1>
  <p class="sub">Notion Pending 扫描队列 · SMS/WA 串行 · Email 并行</p>
  <div id="msg" class="msg"></div>

  <div class="grid" id="stats"></div>
  <div class="health" id="health"></div>

  <div class="card">
    <div class="label">运行参数</div>
    <form class="settings" id="settingsForm">
      <label>扫描间隔（秒）
        <input id="scanInterval" type="number" min="5" step="1" />
      </label>
      <label>SMS/WA 发送间隔（秒）
        <input id="sendGap" type="number" min="5" step="1" />
      </label>
      <label>Bearer Token（保存时需要）
        <input id="token" type="password" placeholder="MONITOR_TOKEN / WA_API_TOKEN" />
      </label>
      <button type="submit">保存</button>
    </form>
    <div class="detail" id="scanMeta"></div>
  </div>

  <div class="card" style="margin-top:14px; overflow:auto;">
    <div class="label">最近执行日志</div>
    <table>
      <thead>
        <tr><th>时间</th><th>事件</th><th>通道</th><th>任务</th><th>结果</th><th>耗时</th></tr>
      </thead>
      <tbody id="logs"></tbody>
    </table>
  </div>
</main>
<script>
async function load() {
  const res = await fetch('/api/monitor/summary');
  const data = await res.json();
  const s = data.stats || {};
  const q = data.queue || {};
  document.getElementById('stats').innerHTML = [
    ['今日领取', s.claimed],
    ['今日执行', s.executed],
    ['成功', s.success],
    ['失败', s.failed],
    ['跳过', s.skipped],
    ['队列积压', q.total],
  ].map(([k,v]) => `<div class="card"><div class="label">${k}</div><div class="value">${v ?? 0}</div></div>`).join('');

  const ch = data.channels || {};
  document.getElementById('health').innerHTML = ['SMS','WHATSAPP','EMAIL'].map(name => {
    const c = ch[name] || {};
    const on = !!c.online;
    return `<div class="card"><div><span class="dot ${on?'on':'off'}"></span><strong>${name}</strong> ${on?'在线':'离线'}</div><div class="detail">${c.detail || ''}${c.pending_jobs!=null?(' · pending jobs '+c.pending_jobs):''}</div></div>`;
  }).join('');

  const settings = data.settings || {};
  document.getElementById('scanInterval').value = settings.scan_interval_seconds || 600;
  document.getElementById('sendGap').value = settings.send_gap_seconds || 90;
  const st = data.scan_state || {};
  document.getElementById('scanMeta').textContent =
    `上次扫描: ${st.last_scan_at || '-'} · found=${st.last_scan_found ?? '-'} enqueued=${st.last_scan_enqueued ?? '-'}`;

  const rows = (data.recent || []).map(r => {
    return `<tr>
      <td>${(r.ts||'').replace('T',' ').slice(0,19)}</td>
      <td>${r.event||''}</td>
      <td>${r.channel||''}</td>
      <td>${(r.title||r.task_id||'').toString().slice(0,42)}</td>
      <td>${r.status||r.reason||''}</td>
      <td>${r.elapsed_ms!=null?(r.elapsed_ms+'ms'):''}</td>
    </tr>`;
  }).join('');
  document.getElementById('logs').innerHTML = rows || '<tr><td colspan="6">暂无日志</td></tr>';
}

document.getElementById('settingsForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const token = document.getElementById('token').value.trim();
  const body = {
    scan_interval_seconds: Number(document.getElementById('scanInterval').value),
    send_gap_seconds: Number(document.getElementById('sendGap').value),
  };
  const headers = {'Content-Type':'application/json'};
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch('/api/monitor/settings', {method:'POST', headers, body: JSON.stringify(body)});
  const data = await res.json().catch(() => ({}));
  document.getElementById('msg').textContent = res.ok ? '已保存，下一轮调度会使用新间隔' : ('保存失败: '+(data.detail||res.status));
  if (res.ok) load();
});

load();
setInterval(load, 15000);
</script>
</body>
</html>
"""


@app.get("/monitor", response_class=HTMLResponse)
def monitor_page() -> HTMLResponse:
    return HTMLResponse(MONITOR_HTML)
