from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import settings

# Persist recent inbound provider ids so SMS Gate webhook retries
# do not re-write Notion / re-POST Portal.
_TTL_SECONDS = 7 * 24 * 3600
_MAX_KEYS = 5000
_lock = threading.Lock()


def _path() -> Path:
    return settings.resolved_data_dir() / "inbound_dedupe.json"


def _load() -> dict[str, float]:
    p = _path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): float(v) for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}
    return {}


def _save(data: dict[str, float]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def seen_or_mark(channel: str, message_id: str | None, *, body: str | None = None, sender: str | None = None) -> bool:
    """Return True if this inbound was already processed (caller should skip)."""
    mid = (message_id or "").strip()
    if mid:
        key = f"{channel}:{mid}"
    else:
        # Fallback when provider id missing: coarse fingerprint.
        b = (body or "").strip()
        s = (sender or "").strip()
        if not b or not s:
            return False
        key = f"{channel}:fp:{s}:{b[:120]}"

    now = time.time()
    with _lock:
        data = _load()
        # drop expired
        data = {k: v for k, v in data.items() if now - v < _TTL_SECONDS}
        if key in data:
            _save(data)
            return True
        data[key] = now
        if len(data) > _MAX_KEYS:
            # keep newest
            items = sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_KEYS]
            data = dict(items)
        _save(data)
        return False


def describe_skip(channel: str, message_id: str | None) -> dict[str, Any]:
    return {
        "ok": True,
        "matched": False,
        "skipped": True,
        "reason": "duplicate_inbound",
        "channel": channel,
        "message_id": message_id,
    }
