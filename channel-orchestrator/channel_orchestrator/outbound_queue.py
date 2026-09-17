from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .channels import normalize_channel
from .config import settings

_lock = threading.Lock()
_ACTIVE = {"queued", "running"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path() -> Path:
    return settings.resolved_data_dir() / "outbound_queue.json"


class OutboundQueue:
    """Persistent FIFO outbound queue (Notion is source of truth; this is runtime only)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _path()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"items": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"items": []}
        if not isinstance(data, dict):
            return {"items": []}
        data.setdefault("items", [])
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_items(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        with _lock:
            items = list(self._load().get("items") or [])
        if active_only:
            return [i for i in items if i.get("status") in _ACTIVE]
        return items

    def active_task_ids(self) -> set[str]:
        return {str(i["task_id"]) for i in self.list_items(active_only=True) if i.get("task_id")}

    def enqueue(
        self,
        *,
        task_id: str,
        channel: str,
        title: str | None = None,
        priority: str | None = None,
        scheduled_at: str | None = None,
    ) -> dict[str, Any] | None:
        channel = normalize_channel(channel)
        with _lock:
            data = self._load()
            items: list[dict[str, Any]] = list(data.get("items") or [])
            for item in items:
                if item.get("task_id") == task_id and item.get("status") in _ACTIVE:
                    return None
            row = {
                "id": str(uuid.uuid4()),
                "task_id": task_id,
                "channel": channel,
                "title": title or task_id,
                "priority": priority or "P2",
                "scheduled_at": scheduled_at,
                "enqueued_at": _now_iso(),
                "status": "queued",
                "notes": None,
                "result": None,
            }
            items.append(row)
            data["items"] = items
            self._save(data)
            return row

    def pop_emails(self) -> list[dict[str, Any]]:
        with _lock:
            data = self._load()
            items: list[dict[str, Any]] = list(data.get("items") or [])
            taken: list[dict[str, Any]] = []
            for item in items:
                if item.get("status") != "queued":
                    continue
                if normalize_channel(str(item.get("channel") or "")) != "EMAIL":
                    continue
                item["status"] = "running"
                item["started_at"] = _now_iso()
                taken.append(dict(item))
            data["items"] = items
            self._save(data)
            return taken

    def pop_next_phone(self) -> dict[str, Any] | None:
        with _lock:
            data = self._load()
            items: list[dict[str, Any]] = list(data.get("items") or [])
            for item in items:
                if item.get("status") != "queued":
                    continue
                ch = normalize_channel(str(item.get("channel") or ""))
                if ch not in {"SMS", "WHATSAPP"}:
                    continue
                item["status"] = "running"
                item["started_at"] = _now_iso()
                data["items"] = items
                self._save(data)
                return dict(item)
            return None

    def finish(self, queue_id: str, *, status: str, notes: str | None = None, result: Any = None) -> None:
        with _lock:
            data = self._load()
            items: list[dict[str, Any]] = list(data.get("items") or [])
            for item in items:
                if item.get("id") != queue_id:
                    continue
                item["status"] = status
                item["finished_at"] = _now_iso()
                item["notes"] = notes
                item["result"] = result
                break
            # Keep last 200 finished rows for monitor; drop older terminal rows.
            active = [i for i in items if i.get("status") in _ACTIVE]
            done = [i for i in items if i.get("status") not in _ACTIVE][-200:]
            data["items"] = active + done
            self._save(data)

    def depth(self) -> dict[str, int]:
        items = self.list_items(active_only=True)
        out = {"total": len(items), "EMAIL": 0, "SMS": 0, "WHATSAPP": 0}
        for item in items:
            ch = normalize_channel(str(item.get("channel") or ""))
            if ch in out:
                out[ch] += 1
        return out


_queue: OutboundQueue | None = None


def get_outbound_queue() -> OutboundQueue:
    global _queue
    if _queue is None:
        _queue = OutboundQueue()
    return _queue
