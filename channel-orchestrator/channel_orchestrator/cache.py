from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .channels import normalize_channel
from .config import settings
from .email_util import normalize_email
from .phone import normalize_e164


class OutboundCache:
    """Last outbound task mapping (channel-separated).

    Lookup key:
      SMS/WA → phone E.164
      EMAIL  → normalized email
    Values include taskId + system threadId (and for email, gmailThreadId).

    WhatsApp marks state=ready at job enqueue (not only after Companion ACK) so
    inbound replies during send still match. Hard failures still mark failed.
    """

    STATE_PENDING = "pending"
    STATE_READY = "ready"
    STATE_FAILED = "failed"

    def __init__(self, channel: str = "SMS", path: Path | None = None) -> None:
        self.channel = normalize_channel(channel)
        suffix = {
            "WHATSAPP": "whatsapp",
            "EMAIL": "email",
            "SMS": "sms",
        }.get(self.channel, self.channel.lower())
        self.path = path or (settings.resolved_data_dir() / f"last_outbound_by_phone_{suffix}.json")
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self.load()

    def _normalize_key(self, raw: str | None) -> str | None:
        if self.channel == "EMAIL":
            return normalize_email(raw)
        return normalize_e164(raw)

    def load(self) -> None:
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            legacy = settings.resolved_data_dir() / "last_outbound_by_phone.json"
            if self.channel == "SMS" and legacy.exists():
                self._data = json.loads(legacy.read_text(encoding="utf-8"))
            else:
                self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _put(
        self,
        party_key: str,
        *,
        state: str,
        task_page_id: str,
        thread_id: str | None,
        contact_page_id: str | None,
        conversation_page_id: str | None,
        sent_at: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = self._normalize_key(party_key) or party_key
        entry: dict[str, Any] = {
            "state": state,
            "task_page_id": task_page_id,
            "thread_id": thread_id,
            "contact_page_id": contact_page_id,
            "conversation_page_id": conversation_page_id,
            "channel": self.channel,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.channel == "EMAIL":
            entry["email"] = key
            entry["our_email"] = settings.gmail_user or None
        else:
            entry["phone_e164"] = key
            entry["our_number"] = settings.saily_phone_e164 or None
        if sent_at is not None:
            entry["sent_at"] = sent_at.isoformat()
        if extra:
            entry.update(extra)
        with self._lock:
            self._data[key] = entry
            self.save()
            return dict(entry)

    def set_pending(
        self,
        party_key: str,
        *,
        task_page_id: str,
        thread_id: str | None,
        contact_page_id: str | None,
        conversation_page_id: str | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._put(
            party_key,
            state=self.STATE_PENDING,
            task_page_id=task_page_id,
            thread_id=thread_id,
            contact_page_id=contact_page_id,
            conversation_page_id=conversation_page_id,
            extra=extra,
        )

    def set_ready(
        self,
        party_key: str,
        *,
        task_page_id: str,
        thread_id: str | None,
        contact_page_id: str | None,
        conversation_page_id: str | None,
        sent_at: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._put(
            party_key,
            state=self.STATE_READY,
            task_page_id=task_page_id,
            thread_id=thread_id,
            contact_page_id=contact_page_id,
            conversation_page_id=conversation_page_id,
            sent_at=sent_at or datetime.now(timezone.utc),
            extra=extra,
        )

    def set_success(
        self,
        party_key: str,
        *,
        task_page_id: str,
        contact_page_id: str | None,
        conversation_page_id: str | None,
        sent_at: datetime | None = None,
        thread_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.set_ready(
            party_key,
            task_page_id=task_page_id,
            thread_id=thread_id,
            contact_page_id=contact_page_id,
            conversation_page_id=conversation_page_id,
            sent_at=sent_at,
            extra=extra,
        )

    def mark_failed(self, party_key: str) -> None:
        key = self._normalize_key(party_key) or party_key
        with self._lock:
            prev = self._data.get(key)
            if not prev:
                return
            prev = dict(prev)
            prev["state"] = self.STATE_FAILED
            prev["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._data[key] = prev
            self.save()

    def get(self, party_raw: str | None) -> dict[str, Any] | None:
        key = self._normalize_key(party_raw)
        if not key:
            return None
        with self._lock:
            entry = self._data.get(key)
            return dict(entry) if entry else None

    def get_ready_for_reply(self, party_raw: str | None) -> dict[str, Any] | None:
        entry = self.get(party_raw)
        if not entry:
            return None
        # ready = confirmed/enqueued mapping; pending = legacy pre-enqueue-ready rows.
        # Both are matchable so WA replies during send still ingest. failed is not.
        if entry.get("state") not in {self.STATE_READY, self.STATE_PENDING}:
            return None
        if not entry.get("task_page_id") or not entry.get("thread_id"):
            return None
        return entry

    def get_ready_by_gmail_thread(self, gmail_thread_id: str | None) -> dict[str, Any] | None:
        if not gmail_thread_id:
            return None
        with self._lock:
            for entry in self._data.values():
                if entry.get("state") != self.STATE_READY:
                    continue
                if (
                    entry.get("gmail_thread_id") == gmail_thread_id
                    or entry.get("gmailThreadId") == gmail_thread_id
                ):
                    if entry.get("task_page_id") and entry.get("thread_id"):
                        return dict(entry)
        return None
