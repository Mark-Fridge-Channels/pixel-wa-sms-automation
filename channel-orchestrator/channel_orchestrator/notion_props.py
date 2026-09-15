from __future__ import annotations

from typing import Any


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
