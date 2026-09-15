from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import settings
from .heartbeat import heartbeat_stale, read_heartbeat, touch_heartbeat
from .inbound import handle_inbound_sms, handle_inbound_whatsapp
from .inbound_dedupe import describe_skip, seen_or_mark
from .outbound import finalize_whatsapp_job
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
