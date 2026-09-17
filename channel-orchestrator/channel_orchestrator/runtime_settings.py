from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .config import settings

_lock = threading.Lock()


def _path() -> Path:
    return settings.resolved_data_dir() / "runtime_settings.json"


def _defaults() -> dict[str, Any]:
    return {
        "scan_interval_seconds": int(settings.scheduler_scan_seconds),
        "send_gap_seconds": int(settings.send_gap_seconds),
    }


def load_runtime_settings() -> dict[str, Any]:
    base = _defaults()
    path = _path()
    if not path.exists():
        return dict(base)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(base)
    if not isinstance(data, dict):
        return dict(base)
    out = dict(base)
    for key in ("scan_interval_seconds", "send_gap_seconds"):
        if key in data:
            try:
                out[key] = max(5, int(data[key]))
            except (TypeError, ValueError):
                pass
    return out


def save_runtime_settings(**updates: Any) -> dict[str, Any]:
    with _lock:
        current = load_runtime_settings()
        for key, value in updates.items():
            if key not in {"scan_interval_seconds", "send_gap_seconds"}:
                continue
            try:
                current[key] = max(5, int(value))
            except (TypeError, ValueError) as e:
                raise ValueError(f"invalid {key}") from e
        _path().write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return current


def get_scan_interval_seconds() -> int:
    return int(load_runtime_settings()["scan_interval_seconds"])


def get_send_gap_seconds() -> int:
    return int(load_runtime_settings()["send_gap_seconds"])
