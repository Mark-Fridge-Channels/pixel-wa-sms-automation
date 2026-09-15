from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings


class WaJobQueue:
    """Simple file-backed WhatsApp send job queue for phone Companion polling."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (settings.resolved_data_dir() / "wa_jobs.json")
        self._lock = threading.Lock()
        self._jobs: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._jobs = raw.get("jobs") or []
        else:
            self._jobs = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": self._jobs}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            # Reload so CLI run-once and serve share the same file-backed queue.
            self.load()
            entry = {
                "id": job.get("id") or str(uuid.uuid4()),
                "status": "queued",  # queued|leased|done|failed
                "created_at": datetime.now(timezone.utc).isoformat(),
                "leased_at": None,
                "finished_at": None,
                **{k: v for k, v in job.items() if k not in {"id", "status"}},
            }
            self._jobs.append(entry)
            self.save()
            return entry

    def _expire_leases(self) -> None:
        now = datetime.now(timezone.utc)
        lease = settings.wa_job_lease_seconds
        for j in self._jobs:
            if j.get("status") != "leased" or not j.get("leased_at"):
                continue
            try:
                leased = datetime.fromisoformat(str(j["leased_at"]).replace("Z", "+00:00"))
            except ValueError:
                j["status"] = "queued"
                j["leased_at"] = None
                continue
            if (now - leased.astimezone(timezone.utc)).total_seconds() > lease:
                j["status"] = "queued"
                j["leased_at"] = None

    def next_job(self) -> dict[str, Any] | None:
        with self._lock:
            self.load()
            self._expire_leases()
            for j in self._jobs:
                if j.get("status") == "queued":
                    j["status"] = "leased"
                    j["leased_at"] = datetime.now(timezone.utc).isoformat()
                    self.save()
                    return dict(j)
            return None

    def complete(
        self,
        job_id: str,
        *,
        ok: bool,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            self.load()
            for j in self._jobs:
                if j.get("id") != job_id:
                    continue
                j["status"] = "done" if ok else "failed"
                j["finished_at"] = datetime.now(timezone.utc).isoformat()
                j["error"] = error
                j["detail"] = detail or {}
                self.save()
                return dict(j)
            return None

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self.load()
            for j in self._jobs:
                if j.get("id") == job_id:
                    return dict(j)
            return None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            self.load()
            return list(self._jobs[-limit:])

    def count_done_today(self, day_yyyy_mm_dd: str) -> int:
        """Count successful WA sends finished on UTC day prefix (approx daily limit)."""
        with self._lock:
            self.load()
            n = 0
            for j in self._jobs:
                if j.get("status") != "done":
                    continue
                fin = str(j.get("finished_at") or "")
                if fin.startswith(day_yyyy_mm_dd):
                    n += 1
            return n


_queue: WaJobQueue | None = None


def get_wa_queue() -> WaJobQueue:
    global _queue
    if _queue is None:
        _queue = WaJobQueue()
    return _queue
