from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .phone import digits_only, normalize_e164_with_reason
from .wa_jobs import get_wa_queue

log = logging.getLogger(__name__)

_lock = threading.Lock()

# has_whatsapp: true | false | null (unknown/pending/error)
STATUS_PENDING = "pending"
STATUS_YES = "yes"
STATUS_NO = "no"
STATUS_UNKNOWN = "unknown"
STATUS_ERROR = "error"


def _state_path():
    return settings.resolved_data_dir() / "wa_probe_state.json"


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"last_enqueue_at": None, "by_phone": {}, "by_id": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_enqueue_at": None, "by_phone": {}, "by_id": {}}
    data.setdefault("last_enqueue_at", None)
    data.setdefault("by_phone", {})
    data.setdefault("by_id", {})
    return data


def _save_state(data: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(
    *,
    probe_id: str,
    phone: str,
    status: str,
    has_whatsapp: bool | None,
    job_id: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "id": probe_id,
        "phone": phone,
        "status": status,
        "has_whatsapp": has_whatsapp,
        "job_id": job_id,
        "detail": detail,
        "updated_at": _now_iso(),
    }


def get_probe(probe_id: str) -> dict[str, Any] | None:
    with _lock:
        state = _load_state()
        row = state["by_id"].get(probe_id)
        return dict(row) if isinstance(row, dict) else None


def get_probe_by_phone(phone: str) -> dict[str, Any] | None:
    e164, _ = normalize_e164_with_reason(phone)
    if not e164:
        return None
    with _lock:
        state = _load_state()
        row = state["by_phone"].get(e164)
        return dict(row) if isinstance(row, dict) else None


def enqueue_probe(phone_raw: str) -> dict[str, Any]:
    """
    Enqueue a Companion probe job (open chat, do not send).
    Enforces global min interval between probe enqueues (default 120s).
    Does not touch Notion tasks.
    """
    e164, reason = normalize_e164_with_reason(phone_raw)
    if not e164:
        return {"ok": False, "error": reason or "invalid phone", "status_code": 400}

    # Use explicit int — do not use `or 120` (0 is a valid "no interval" for tests).
    interval = max(0, int(settings.wa_probe_interval_seconds))
    with _lock:
        state = _load_state()
        # Reuse in-flight probe for same phone.
        existing = state["by_phone"].get(e164)
        if isinstance(existing, dict) and existing.get("status") == STATUS_PENDING:
            return {
                "ok": True,
                "queued": False,
                "reused": True,
                "probe": existing,
                "retry_after_seconds": 0,
            }

        last = state.get("last_enqueue_at")
        if last and interval > 0:
            try:
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds()
            except ValueError:
                elapsed = interval
            if elapsed < interval:
                wait = int(interval - elapsed) + 1
                return {
                    "ok": False,
                    "error": f"probe interval {interval}s; retry after {wait}s",
                    "retry_after_seconds": wait,
                    "status_code": 429,
                    "last_probe": existing,
                }

        job = get_wa_queue().enqueue(
            {
                "phone": e164,
                "text": "",
                "job_type": "probe",
                "channel": "WHATSAPP",
                "task_id": None,
            }
        )
        probe_id = str(job["id"])
        row = _row(
            probe_id=probe_id,
            phone=e164,
            status=STATUS_PENDING,
            has_whatsapp=None,
            job_id=probe_id,
            detail="queued",
        )
        state["last_enqueue_at"] = _now_iso()
        state["by_id"][probe_id] = row
        state["by_phone"][e164] = row
        _save_state(state)
        log.info("wa probe enqueued id=%s phone=%s", probe_id, e164)
        return {
            "ok": True,
            "queued": True,
            "reused": False,
            "probe": row,
            "retry_after_seconds": interval,
            "digits": digits_only(e164),
        }


def apply_probe_result(
    job_id: str,
    *,
    has_whatsapp: bool | None,
    status: str | None = None,
    detail: str | None = None,
    ok: bool = True,
) -> dict[str, Any] | None:
    """Update probe store from Companion job result."""
    if status is None:
        if has_whatsapp is True:
            status = STATUS_YES
        elif has_whatsapp is False:
            status = STATUS_NO
        elif ok:
            status = STATUS_UNKNOWN
        else:
            status = STATUS_ERROR
    with _lock:
        state = _load_state()
        row = state["by_id"].get(job_id)
        if not isinstance(row, dict):
            # Still record if job_id unknown but we have phone later — skip.
            return None
        phone = str(row.get("phone") or "")
        updated = _row(
            probe_id=job_id,
            phone=phone,
            status=status,
            has_whatsapp=has_whatsapp,
            job_id=job_id,
            detail=detail,
        )
        state["by_id"][job_id] = updated
        if phone:
            state["by_phone"][phone] = updated
        _save_state(state)
        return updated
