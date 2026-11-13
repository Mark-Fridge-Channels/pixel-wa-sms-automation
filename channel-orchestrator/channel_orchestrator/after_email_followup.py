"""Gate SMS/WhatsApp tasks that must wait for an email Task to finish."""

from __future__ import annotations

from typing import Any


def after_task_id_from_extended(extended: dict[str, Any] | None) -> str | None:
    if not extended:
        return None
    raw = extended.get("afterTaskId") or extended.get("after_task_id")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def dependency_block_reason(notion: Any, extended: dict[str, Any] | None) -> str | None:
    """If non-None, execute_task should skip or cancel instead of sending."""
    after_id = after_task_id_from_extended(extended)
    if not after_id:
        return None
    status = notion.get_task_status(after_id) or ""
    if status == "Completed":
        return None
    if status in {"Failed", "Cancelled"}:
        return f"afterTaskId {after_id} is {status}"
    return f"waiting for afterTaskId {after_id} (status={status or 'unknown'})"


def should_cancel_for_failed_email(reason: str | None) -> bool:
    if not reason:
        return False
    return " is Failed" in reason or " is Cancelled" in reason
