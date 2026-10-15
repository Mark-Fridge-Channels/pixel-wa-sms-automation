"""Register emails sent outside this orchestrator (e.g. superExhibition) into the Email outbound cache.

The caller sends the email, writes the Completed Task/Conversation to Notion, then registers here.
After that, replies / bounces match exactly like orchestrator-sent mail.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .cache import OutboundCache
from .email_util import normalize_email
from .exec_log import append_exec_event

log = logging.getLogger(__name__)


class ExternalSendError(ValueError):
    pass


def _parse_sent_at(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except ValueError as e:
            raise ExternalSendError(f"sentAt 不是 ISO 时间: {raw!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _gmail_meta(gmail: Any, gmail_message_id: str) -> dict[str, Any]:
    msg = gmail.get_message(gmail_message_id, format="metadata")
    headers = {
        str(h.get("name") or "").lower(): str(h.get("value") or "")
        for h in (msg.get("payload") or {}).get("headers") or []
    }
    sent_at = None
    if msg.get("internalDate"):
        try:
            sent_at = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            sent_at = None
    return {
        "gmail_thread_id": msg.get("threadId") or None,
        "subject": headers.get("subject") or None,
        "sent_at": sent_at,
    }


def register_external_email_sent(
    *,
    email: str | None,
    task_id: str | None,
    thread_id: str | None,
    gmail_thread_id: str | None = None,
    gmail_message_id: str | None = None,
    subject: str | None = None,
    sent_at: Any = None,
    contact_page_id: str | None = None,
    conversation_page_id: str | None = None,
    source: str = "external",
    cache: OutboundCache | None = None,
    gmail: Any | None = None,
) -> dict[str, Any]:
    """Write a ready Email cache row for a message another system already sent.

    Never replaces a row for a different task that was sent later than this one.
    """
    to = normalize_email(email)
    task_id = str(task_id or "").strip()
    thread_id = str(thread_id or "").strip()
    if not to:
        raise ExternalSendError(f"email 无效: {email!r}")
    if not task_id:
        raise ExternalSendError("taskId 必填")
    if not thread_id:
        raise ExternalSendError("threadId 必填")

    gmail_thread_id = str(gmail_thread_id or "").strip() or None
    gmail_message_id = str(gmail_message_id or "").strip() or None
    subject = str(subject or "").strip() or None
    sent = _parse_sent_at(sent_at)

    if gmail_message_id and not (gmail_thread_id and subject and sent):
        try:
            from .gmail_client import GmailClient

            meta = _gmail_meta(gmail or GmailClient(), gmail_message_id)
            gmail_thread_id = gmail_thread_id or meta["gmail_thread_id"]
            subject = subject or meta["subject"]
            sent = sent or meta["sent_at"]
        except Exception:  # noqa: BLE001
            log.warning("gmail lookup failed for external send id=%s", gmail_message_id, exc_info=True)
    sent = sent or datetime.now(timezone.utc)

    from .email_suppression import EmailSuppressionList, blocked_notes

    bounced = EmailSuppressionList().get(to)
    if bounced:
        log.warning("external send to hard-bounced address email=%s task=%s", to, task_id)

    cache = cache or OutboundCache(channel="EMAIL")
    prev = cache.get(to)
    if prev and prev.get("task_page_id") != task_id:
        prev_sent = _parse_sent_at(prev.get("sent_at") or prev.get("updated_at"))
        if prev_sent and prev_sent > sent:
            log.info(
                "external send skipped email=%s task=%s: newer task %s sent at %s",
                to,
                task_id,
                prev.get("task_page_id"),
                prev_sent.isoformat(),
            )
            return {
                "ok": True,
                "registered": False,
                "reason": "newer_outbound_exists",
                "email": to,
                "task_id": task_id,
                "current_task_id": prev.get("task_page_id"),
                "current_sent_at": prev_sent.isoformat(),
            }

    extra: dict[str, Any] = {"source": source}
    if gmail_thread_id:
        extra.update({"gmail_thread_id": gmail_thread_id, "gmailThreadId": gmail_thread_id})
    if gmail_message_id:
        extra.update({"gmail_message_id": gmail_message_id, "gmailMessageId": gmail_message_id})
    if subject:
        extra["subject"] = subject
    entry = cache.set_ready(
        to,
        task_page_id=task_id,
        thread_id=thread_id,
        contact_page_id=contact_page_id,
        conversation_page_id=conversation_page_id,
        sent_at=sent,
        extra=extra,
    )
    append_exec_event(
        {
            "event": "external_sent",
            "channel": "EMAIL",
            "status": "registered",
            "ok": True,
            "task_id": task_id,
            "title": f"{to} · {subject or ''}"[:120],
            "source": source,
        }
    )
    return {
        "ok": True,
        "registered": True,
        "email": to,
        "task_id": task_id,
        "thread_id": thread_id,
        "gmail_thread_id": gmail_thread_id,
        "subject": subject,
        "sent_at": entry.get("sent_at"),
        "suppressed": bool(bounced),
        "warning": blocked_notes(to, bounced) if bounced else None,
    }
