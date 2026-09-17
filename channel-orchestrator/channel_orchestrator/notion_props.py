from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo


def rich_text_plain(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    chunks = prop.get("rich_text") or prop.get("title") or []
    return "".join(c.get("plain_text", "") for c in chunks).strip()


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
