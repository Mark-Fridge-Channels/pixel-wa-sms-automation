from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

_lock = threading.Lock()


def _path() -> Path:
    return settings.resolved_data_dir() / "exec_log.jsonl"


def _state_path() -> Path:
    return settings.resolved_data_dir() / "scan_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_exec_event(event: dict[str, Any]) -> dict[str, Any]:
    row = {"ts": _now_iso(), **event}
    with _lock:
        with _path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def read_recent_events(limit: int = 50) -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-max(1, limit) :]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))


def today_stats(*, day: str | None = None) -> dict[str, Any]:
    """Aggregate today's scan/exec outcomes from exec_log.jsonl (UTC day)."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = _path()
    tallies = {
        "claimed": 0,
        "executed": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "uncertain": 0,
        "by_channel": {},
    }
    if not path.exists():
        return {"day": day, **tallies}

    def bump(channel: str | None, key: str) -> None:
        tallies[key] = int(tallies[key]) + 1
        if not channel:
            return
        ch = tallies["by_channel"].setdefault(
            channel, {"claimed": 0, "executed": 0, "success": 0, "failed": 0, "skipped": 0, "uncertain": 0}
        )
        ch[key] = int(ch.get(key, 0)) + 1

    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = str(row.get("ts") or "")
        if not ts.startswith(day):
            continue
        event = str(row.get("event") or "")
        channel = row.get("channel")
        if event == "enqueued":
            bump(channel, "claimed")
        elif event == "finished":
            bump(channel, "executed")
            status = str(row.get("status") or "")
            if status in {"completed", "queued", "dry_run", "ok"}:
                bump(channel, "success")
            elif status in {"failed", "uncertain"}:
                bump(channel, "failed" if status == "failed" else "uncertain")
            elif status == "skipped":
                bump(channel, "skipped")
            else:
                bump(channel, "failed" if not row.get("ok", True) else "success")
    return {"day": day, **tallies}


def load_scan_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def touch_scan_state(**updates: Any) -> dict[str, Any]:
    with _lock:
        data = load_scan_state()
        data.update(updates)
        data["updated_at"] = _now_iso()
        _state_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
