from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .gateway import SmsGatewayClient
from .heartbeat import heartbeat_stale, read_heartbeat

log = logging.getLogger(__name__)

_lock = threading.Lock()
_last_eval_mono = 0.0


def _state_path():
    return settings.resolved_data_dir() / "device_alert_state.json"


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    _state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def diagnose_phone(row: dict[str, Any] | None = None) -> list[str]:
    """Return machine-readable reason codes for current phone/Companion health."""
    row = row if row is not None else (read_heartbeat().get("phone") or {})
    reasons: list[str] = []
    if heartbeat_stale("phone"):
        reasons.append("heartbeat_stale")
    if row.get("automation_on") is False:
        reasons.append("automation_off")
    if row.get("poll_alive") is False:
        reasons.append("poll_dead")
    if row.get("a11y_bound") is False:
        reasons.append("a11y_unbound")
    if row.get("google_ok") is False:
        reasons.append("network_or_vpn_down")
    if row.get("orch_ok") is False and row.get("google_ok") is True:
        reasons.append("orch_unreachable_from_phone")
    return reasons


def _format_sms(reasons: list[str], row: dict[str, Any]) -> str:
    last = str(row.get("last_seen") or "never")
    detail = str(row.get("vpn_detail") or "")[:80]
    ver = str(row.get("app_version") or "?")
    return (
        f"[WA Companion ALERT] phone unhealthy: {', '.join(reasons)}. "
        f"last_seen={last} ver={ver} {detail}"
    )[:300]


def maybe_alert_device_health(*, force_eval: bool = False) -> dict[str, Any]:
    """
    If phone/Companion looks unhealthy longer than grace, SMS ops (cooldown applied).
    Safe to call frequently; internally rate-limited to ~60s.
    """
    global _last_eval_mono
    to = (settings.alert_sms_to or "").strip()
    if not to:
        return {"ok": True, "skipped": "alert_sms_to empty"}

    now_mono = time.monotonic()
    if not force_eval and now_mono - _last_eval_mono < 60:
        return {"ok": True, "skipped": "rate_limit"}
    _last_eval_mono = now_mono

    with _lock:
        row = dict(read_heartbeat().get("phone") or {})
        reasons = diagnose_phone(row)
        state = _load_state()
        grace_min = max(1, int(settings.alert_unhealthy_grace_minutes or 20))
        cooldown_min = max(5, int(settings.alert_sms_cooldown_minutes or 60))

        if not reasons:
            state["unhealthy_since"] = None
            state["last_reasons"] = []
            _save_state(state)
            return {"ok": True, "healthy": True}

        since = state.get("unhealthy_since")
        if not since:
            state["unhealthy_since"] = _now_iso()
            state["last_reasons"] = reasons
            _save_state(state)
            return {"ok": True, "unhealthy": True, "waiting_grace": True, "reasons": reasons}

        try:
            since_dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - since_dt.astimezone(timezone.utc)).total_seconds() / 60
        except ValueError:
            age_min = grace_min + 1

        state["last_reasons"] = reasons
        if age_min < grace_min:
            _save_state(state)
            return {
                "ok": True,
                "unhealthy": True,
                "waiting_grace": True,
                "age_min": round(age_min, 1),
                "grace_min": grace_min,
                "reasons": reasons,
            }

        last_sent = state.get("last_alert_at")
        if last_sent:
            try:
                sent_dt = datetime.fromisoformat(str(last_sent).replace("Z", "+00:00"))
                since_sent = (
                    datetime.now(timezone.utc) - sent_dt.astimezone(timezone.utc)
                ).total_seconds() / 60
                if since_sent < cooldown_min:
                    _save_state(state)
                    return {
                        "ok": True,
                        "unhealthy": True,
                        "skipped": "cooldown",
                        "reasons": reasons,
                        "cooldown_remaining_min": round(cooldown_min - since_sent, 1),
                    }
            except ValueError:
                pass

        text = _format_sms(reasons, row)
        try:
            resp = SmsGatewayClient().send_sms([to], text)
            state["last_alert_at"] = _now_iso()
            state["last_alert_text"] = text
            state["last_alert_reasons"] = reasons
            _save_state(state)
            log.warning("device alert SMS sent to %s reasons=%s", to, reasons)
            return {"ok": True, "sent": True, "to": to, "reasons": reasons, "gateway": resp}
        except Exception as e:  # noqa: BLE001
            log.exception("device alert SMS failed")
            state["last_alert_error"] = str(e)
            state["last_alert_error_at"] = _now_iso()
            _save_state(state)
            return {"ok": False, "error": str(e), "reasons": reasons}
