from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import settings
from .exec_log import load_scan_state, log_inbound_event, read_recent_events, today_stats
from .gateway import SmsGatewayClient
from .heartbeat import heartbeat_stale, read_heartbeat, touch_heartbeat
from .inbound import handle_inbound_sms, handle_inbound_whatsapp
from .inbound_dedupe import describe_skip, seen_or_mark
from .outbound import finalize_whatsapp_job
from .outbound_queue import get_outbound_queue
from .phone import normalize_e164
from .runtime_settings import load_runtime_settings, save_runtime_settings
from .wa_jobs import get_wa_queue

log = logging.getLogger(__name__)

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
    google_ok = phone_hb.get("google_ok")
    vpn_transport = phone_hb.get("vpn_transport")
    wa_online = phone_online if google_ok is None else bool(phone_online and google_ok)
    wa_detail = phone_hb.get("last_seen") or "no heartbeat"
    extra_bits = []
    if google_ok is not None:
        extra_bits.append("google=" + ("ok" if google_ok else "fail"))
    if vpn_transport is not None:
        extra_bits.append("vpn=" + ("up" if vpn_transport else "down"))
    if phone_hb.get("always_on_vpn") is False:
        extra_bits.append("always-on off")
    if extra_bits:
        wa_detail = f"{wa_detail} · " + " ".join(extra_bits)
    wa = {
        "channel": "WHATSAPP",
        "online": wa_online,
        "phone_online": phone_online,
        "pending_jobs": pending_wa,
        "google_ok": google_ok,
        "vpn_transport": vpn_transport,
        "always_on_vpn": phone_hb.get("always_on_vpn"),
        "detail": wa_detail,
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
        log_inbound_event(
            channel="SMS",
            status="duplicate",
            sender=normalize_e164(normalized.get("sender")) or str(normalized.get("sender") or "") or None,
            body=str(normalized.get("body") or ""),
            reason="duplicate_inbound",
            matched=False,
            ok=True,
        )
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
        log_inbound_event(
            channel="WHATSAPP",
            status="duplicate",
            sender=normalize_e164(normalized.get("sender")) or str(normalized.get("sender") or "") or None,
            body=str(normalized.get("body") or ""),
            reason="duplicate_inbound",
            matched=False,
            ok=True,
        )
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


@app.post("/webhook/whatsapp/media")
async def whatsapp_media_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Companion uploads inbound WA media; orch stores on S3 then Portal reply."""
    _check_wa_token(authorization)
    form = await request.form()
    sender = str(form.get("from") or form.get("sender") or "").strip()
    caption = str(form.get("body") or form.get("text") or form.get("caption") or "").strip()
    media_type = str(form.get("media_type") or form.get("mediaType") or "file").strip().lower()
    received_at = str(form.get("received_at") or "").strip() or None
    message_id = str(form.get("message_id") or form.get("messageId") or "").strip() or None
    upload = form.get("file")
    if upload is None:
        raise HTTPException(status_code=400, detail="file required")

    filename = getattr(upload, "filename", None) or f"wa-{media_type}.bin"
    content_type = getattr(upload, "content_type", None)
    data = await upload.read()  # type: ignore[union-attr]
    if not data:
        raise HTTPException(status_code=400, detail="empty file")

    from .s3_media import S3Uploader, content_placeholder, media_type_normalize, mime_for_media_type

    mtype = media_type_normalize(media_type) or "file"
    ctype = content_type or mime_for_media_type(mtype, str(filename))
    up = S3Uploader().upload_bytes(
        data,
        filename=str(filename),
        content_type=ctype,
        media_type=mtype,
    )
    if not up.get("ok"):
        raise HTTPException(status_code=502, detail=up.get("error") or "s3 upload failed")

    media_url = up.get("mediaUrl") or ""
    caption_or_placeholder = caption or content_placeholder(mtype)
    if media_url:
        if not caption or caption_or_placeholder in {
            "[image]", "[video]", "[audio]", "[file]", "[media]", f"[{mtype}]"
        }:
            body_content = media_url
        elif media_url not in caption_or_placeholder:
            body_content = f"{caption_or_placeholder}\n{media_url}"
        else:
            body_content = caption_or_placeholder
    else:
        body_content = caption_or_placeholder

    normalized = {
        "sender": sender,
        "from": sender,
        "body": body_content,
        "message_id": message_id,
        "received_at": received_at,
        "media_url": media_url or None,
        "media_type": mtype,
        "media_content_type": up.get("contentType"),
        "media_filename": up.get("filename"),
    }
    event = {
        "id": str(uuid.uuid4()),
        "channel": "WHATSAPP",
        "received_at_host": _now_iso(),
        "normalized": normalized,
        "s3": {k: up.get(k) for k in ("mediaUrl", "s3Key", "s3Bucket", "size")},
    }
    INBOUND.append(event)
    _append_log(event)
    if seen_or_mark(
        "WHATSAPP",
        message_id,
        body=f"{mtype}:{up.get('s3Key')}:{caption}",
        sender=sender,
    ):
        touch_heartbeat("phone")
        log_inbound_event(
            channel="WHATSAPP",
            status="duplicate",
            sender=normalize_e164(sender) or sender or None,
            body=caption or f"[{mtype}]",
            reason="duplicate_inbound",
            matched=False,
            ok=True,
        )
        return JSONResponse(describe_skip("WHATSAPP", message_id))
    result = handle_inbound_whatsapp(normalized)
    touch_heartbeat("phone")
    return JSONResponse(
        {
            "ok": True,
            "id": event["id"],
            "matched": result.get("matched"),
            "task_page_id": result.get("task_page_id"),
            "mediaUrl": up.get("mediaUrl"),
            "mediaType": mtype,
            "webhook": result.get("webhook"),
        }
    )


@app.get("/wa/jobs/next")
def wa_job_next(authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_wa_token(authorization)
    touch_heartbeat("phone")
    job = get_wa_queue().next_job()
    if not job:
        return JSONResponse({"ok": True, "job": None})
    # companion needs phone digits + text (+ optional media)
    return JSONResponse(
        {
            "ok": True,
            "job": {
                "id": job["id"],
                "phone": job.get("phone"),
                "text": job.get("text"),
                "task_id": job.get("task_id"),
                "media_url": job.get("media_url"),
                "media_type": job.get("media_type"),
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


@app.post("/wa/jobs")
async def wa_job_enqueue(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Manual enqueue for ops/tests. Body: {phone, text?, media_url?, media_type?, task_id?}."""
    _check_wa_token(authorization)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected json object")
    phone = str(body.get("phone") or "").strip()
    text = str(body.get("text") or "").strip()
    media_url = str(body.get("media_url") or body.get("mediaUrl") or "").strip()
    media_type = str(body.get("media_type") or body.get("mediaType") or "").strip().lower()
    if not phone:
        raise HTTPException(status_code=400, detail="phone required")
    if not text and not media_url:
        raise HTTPException(status_code=400, detail="text or media_url required")
    if media_url and media_type not in {"image", "video"}:
        raise HTTPException(status_code=400, detail="media_type must be image|video when media_url set")
    job_body: dict[str, Any] = {
        "phone": phone,
        "text": text,
        "task_id": body.get("task_id"),
        "channel": "WHATSAPP",
    }
    if media_url:
        job_body["media_url"] = media_url
        job_body["media_type"] = media_type
    job = get_wa_queue().enqueue(job_body)
    return JSONResponse({"ok": True, "job": job})


@app.post("/wa/heartbeat")
async def wa_heartbeat(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _check_wa_token(authorization)
    extra: dict[str, Any] = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            extra = raw
    except Exception:  # noqa: BLE001
        extra = {}
    touch_heartbeat("phone", extra or None)
    # Opportunistic health SMS (rate-limited inside).
    try:
        from .device_alert import maybe_alert_device_health

        maybe_alert_device_health()
    except Exception:  # noqa: BLE001
        log.exception("device alert after heartbeat failed")
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
        "recent": read_recent_events(80),
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
  <h1>Channel Monitor</h1>
  <p class="sub">出站扫描队列 · 入站回复监听 · SMS/WA 串行 · Email 并行</p>
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
    <div class="label">最近日志（出站 + 入站回复）</div>
    <table>
      <thead>
        <tr><th>时间</th><th>事件</th><th>通道</th><th>任务 / 来源</th><th>结果</th><th>耗时</th></tr>
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
    ['入站回复', s.inbound],
    ['队列积压', q.total],
  ].map(([k,v]) => `<div class="card"><div class="label">${k}</div><div class="value">${v ?? 0}</div></div>`).join('');

  const ch = data.channels || {};
  document.getElementById('health').innerHTML = ['SMS','WHATSAPP','EMAIL'].map(name => {
    const c = ch[name] || {};
    const on = !!c.online;
    return `<div class="card"><div><span class="dot ${on?'on':'off'}"></span><strong>${name}</strong> ${on?'在线':'离线'}</div><div class="detail">${c.detail || ''}${c.pending_jobs!=null?(' · pending jobs '+c.pending_jobs):''}${c.google_ok===false?' · Google 不通':''}${c.vpn_transport===false?' · VPN 隧道未起':''}</div></div>`;
  }).join('');

  const settings = data.settings || {};
  document.getElementById('scanInterval').value = settings.scan_interval_seconds || 600;
  document.getElementById('sendGap').value = settings.send_gap_seconds || 90;
  const st = data.scan_state || {};
  document.getElementById('scanMeta').textContent =
    `上次扫描: ${st.last_scan_at || '-'} · found=${st.last_scan_found ?? '-'} enqueued=${st.last_scan_enqueued ?? '-'}`;

  const rows = (data.recent || []).map(r => {
    const result = r.event === 'inbound'
      ? `${r.status||''}${r.reason ? (' · '+r.reason) : ''}`
      : (r.status||r.reason||'');
    const who = r.event === 'inbound'
      ? (r.title || r.sender || r.task_id || '')
      : (r.title || r.task_id || '');
    return `<tr>
      <td>${(r.ts||'').replace('T',' ').slice(0,19)}</td>
      <td>${r.event||''}</td>
      <td>${r.channel||''}</td>
      <td>${String(who).slice(0,48)}</td>
      <td>${String(result).slice(0,48)}</td>
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
