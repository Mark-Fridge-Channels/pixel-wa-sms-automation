from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Notion: ≤2000 chars per rich_text object, ≤100 objects per property.
_RICH_TEXT_CHUNK = 1900
NOTES_MAX_CHARS = 19000
_NOTE_ENTRY_RE = re.compile(r"^【(?:Pending|In Progress|Completed|Failed) · [^】\n]+】$", re.M)


def rich_text_plain(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    chunks = prop.get("rich_text") or prop.get("title") or []
    return "".join(c.get("plain_text", "") for c in chunks).strip()


def rich_text_chunks(text: str) -> list[dict[str, Any]]:
    return [
        {"text": {"content": text[i : i + _RICH_TEXT_CHUNK]}}
        for i in range(0, len(text), _RICH_TEXT_CHUNK)
    ] or [{"text": {"content": ""}}]


def latest_note_entry(notes: str | None) -> str:
    """Body of the newest status entry appended by append_note_entry (whole text if none)."""
    text = notes or ""
    last = None
    for last in _NOTE_ENTRY_RE.finditer(text):
        pass
    return (text[last.end() :] if last else text).strip()


def append_note_entry(existing: str | None, status: str, note: str, at: datetime, tz: str) -> str:
    """Append a status entry; keeps the original Notes and drops the oldest entries past the limit."""
    base = (existing or "").strip()
    note = note.strip()
    if not note:
        return base
    headers = list(_NOTE_ENTRY_RE.finditer(base))
    if headers and latest_note_entry(base) == note and f"【{status} · " in headers[-1].group(0):
        return base
    stamp = at.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")
    combined = f"{base}\n\n【{status} · {stamp}】\n{note}".strip()
    while len(combined) > NOTES_MAX_CHARS:
        entries = list(_NOTE_ENTRY_RE.finditer(combined))
        if len(entries) < 2:
            return combined[-NOTES_MAX_CHARS:]
        combined = (combined[: entries[0].start()] + combined[entries[1].start() :]).strip()
    return combined


def select_name(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    if prop.get("type") == "status" and prop.get("status"):
        return prop["status"].get("name")
    if prop.get("type") == "select" and prop.get("select"):
        return prop["select"].get("name")
    # Fallback shapes
    if prop.get("status"):
        return prop["status"].get("name")
    if prop.get("select"):
        return prop["select"].get("name")
    return None


def date_start(prop: dict[str, Any] | None) -> str | None:
    if not prop or not prop.get("date"):
        return None
    return prop["date"].get("start")


def parse_notion_datetime(
    prop: dict[str, Any] | None,
    *,
    default_tz: str = "America/New_York",
) -> datetime | None:
    """Parse Notion date (optionally with time + time_zone) into aware datetime."""
    if not prop or not prop.get("date"):
        return None
    raw = prop["date"]
    start = raw.get("start")
    if not start:
        return None
    tz_name = raw.get("time_zone") or default_tz
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo(default_tz)

    text = str(start).replace("Z", "+00:00")
    if "T" in text:
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz)
        return dt

    try:
        day = date.fromisoformat(text[:10])
    except ValueError:
        return None
    return datetime.combine(day, time.min, tzinfo=tz)


def is_scheduled_due(
    prop: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    default_tz: str = "America/New_York",
) -> bool:
    scheduled = parse_notion_datetime(prop, default_tz=default_tz)
    if scheduled is None:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return scheduled.astimezone(timezone.utc) <= now.astimezone(timezone.utc)


def relation_ids(prop: dict[str, Any] | None) -> list[str]:
    if not prop:
        return []
    return [r["id"] for r in (prop.get("relation") or []) if r.get("id")]


def phone_value(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    if prop.get("type") == "phone_number":
        return prop.get("phone_number")
    # sometimes stored as rich_text
    if prop.get("rich_text"):
        return rich_text_plain(prop) or None
    return prop.get("phone_number")


def props(page: dict[str, Any]) -> dict[str, Any]:
    return page.get("properties") or {}
