"""Email addresses that hard-bounced. Sends to them are blocked until the address is fixed or removed.

Keyed by the exact normalized address: jshaw@getgrinds.con is blocked, jshaw@getgrinds.com is not.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import settings
from .email_util import normalize_email
from .file_lock import locked_path


class EmailSuppressionList:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (settings.resolved_data_dir() / "email_suppression.json")

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(
        self,
        email: str | None,
        *,
        reason: str,
        kind: str = "hard_bounce",
        task_id: str | None = None,
        gmail_thread_ids: Iterable[str | None] = (),
        source: str = "bounce",
    ) -> dict[str, Any] | None:
        key = normalize_email(email)
        if not key:
            return None
        now = datetime.now(timezone.utc).isoformat()
        threads = [t for t in gmail_thread_ids if t]
        with locked_path(self.path):
            data = self._load()
            prev = data.get(key) or {}
            entry = {
                "email": key,
                "kind": kind,
                "reason": reason or prev.get("reason") or kind,
                "source": source,
                "first_at": prev.get("first_at") or now,
                "last_at": now,
                "count": int(prev.get("count") or 0) + 1,
                "task_ids": _merge(prev.get("task_ids"), [task_id]),
                "gmail_thread_ids": _merge(prev.get("gmail_thread_ids"), threads),
            }
            data[key] = entry
            self._save(data)
            return dict(entry)

    def get(self, email: str | None) -> dict[str, Any] | None:
        key = normalize_email(email)
        if not key:
            return None
        with locked_path(self.path):
            entry = self._load().get(key)
        return dict(entry) if entry else None

    def remove(self, email: str | None) -> bool:
        key = normalize_email(email)
        if not key:
            return False
        with locked_path(self.path):
            data = self._load()
            if key not in data:
                return False
            del data[key]
            self._save(data)
            return True

    def list(self) -> list[dict[str, Any]]:
        with locked_path(self.path):
            data = self._load()
        return sorted((dict(v) for v in data.values()), key=lambda e: e.get("last_at") or "", reverse=True)

    def entry_for_thread(self, gmail_thread_id: str | None) -> dict[str, Any] | None:
        """Suppressed address whose bounce happened in this Gmail thread."""
        if not gmail_thread_id:
            return None
        with locked_path(self.path):
            data = self._load()
        for entry in data.values():
            if gmail_thread_id in (entry.get("gmail_thread_ids") or []):
                return dict(entry)
        return None


def _merge(existing: Any, extra: Iterable[str | None]) -> list[str]:
    out = [str(x) for x in (existing or []) if x]
    for x in extra:
        if x and x not in out:
            out.append(str(x))
    return out[-20:]


def blocked_notes(email: str, entry: dict[str, Any]) -> str:
    when = str(entry.get("last_at") or "")[:10]
    reason = entry.get("reason") or "硬退信"
    return (
        f"Email 未发送：{email} 之前硬退信（{when} · {reason}）。"
        "请重新找邮箱，更新 Key Person 的 Email 后把任务改回 Pending。"
    )
