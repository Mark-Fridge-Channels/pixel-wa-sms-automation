from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from .config import settings

_lock = threading.Lock()
_VPN_KEYS = (
    "vpn_transport",
    "google_ok",
    "orch_ok",
    "always_on_vpn",
    "vpn_recovered",
    "vpn_detail",
)


def touch_heartbeat(device: str = "phone", extra: dict[str, Any] | None = None) -> None:
    path = settings.resolved_data_dir() / "device_heartbeat.json"
    with _lock:
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        prev = data.get(device)
        row: dict[str, Any] = dict(prev) if isinstance(prev, dict) else {}
        row["last_seen"] = datetime.now(timezone.utc).isoformat()
        if extra:
            for key in _VPN_KEYS:
                if key in extra:
                    row[key] = extra[key]
        data[device] = row
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_heartbeat() -> dict[str, Any]:
    path = settings.resolved_data_dir() / "device_heartbeat.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def heartbeat_stale(device: str = "phone", minutes: int | None = None) -> bool:
    minutes = minutes if minutes is not None else settings.device_heartbeat_stale_minutes
    data = read_heartbeat().get(device) or {}
    raw = data.get("last_seen")
    if not raw:
        return True
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
    return age > minutes * 60
