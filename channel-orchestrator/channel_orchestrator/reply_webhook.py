from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


class ReplyWebhookClient:
    """Portal Reply ingest: POST /api/replies (see docs/PLAN_REPLY_INGEST.md)."""

    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        raw = settings.reply_webhook_url if url is None else url
        self.url = raw.rstrip("/") if raw else ""
        self.token = (
            settings.reply_webhook_token if token is None else token
        ) or "local-reply-ingest"

    def send(self, event: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible entry: prefer build_and_send via inbound helpers."""
        if event.get("skip_portal"):
            return {"ok": True, "skipped": True, "reason": event.get("skip_reason") or "skipped"}
        return self.post_reply(event)

    def post_reply(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.url:
            log.info(
                "REPLY_WEBHOOK_URL empty; skip replies taskId=%s",
                payload.get("taskId"),
            )
            return {"ok": True, "skipped": True, "reason": "no_url"}

        # Enforce contract: never send provider message ids as top-level messageId
        body = {
            "taskId": payload.get("taskId") or payload.get("task_page_id"),
            "threadId": payload.get("threadId") or payload.get("thread_id"),
            "content": payload.get("content") or payload.get("body") or "",
            "channel": payload.get("channel"),
            "sender": payload.get("sender") or payload.get("from") or "",
            "messageId": "",
            "occurredAt": payload.get("occurredAt") or payload.get("received_at") or "",
            "extendedParameters": payload.get("extendedParameters")
            or payload.get("extended_parameters")
            or {},
        }
        if payload.get("subject") is not None or payload.get("channel") == "Email":
            body["subject"] = payload.get("subject") or ""
        if not body["taskId"] or not body["threadId"] or not body["content"]:
            return {
                "ok": False,
                "skipped": True,
                "reason": "missing_taskId_threadId_or_content",
                "payload": body,
            }
        if body["channel"] not in {"SMS", "WhatsApp", "Email", "LinkedIn", "Phone"}:
            return {"ok": False, "reason": f"invalid_channel:{body['channel']}"}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.post(self.url, json=body, headers=headers)
                return {
                    "ok": r.status_code < 400,
                    "status_code": r.status_code,
                    "body": r.text[:800],
                    "payload": body,
                }
        except Exception as e:  # noqa: BLE001
            log.exception("POST /api/replies failed")
            return {"ok": False, "error": str(e), "payload": body}

    def build_sms_payload(
        self,
        *,
        task_id: str,
        thread_id: str,
        content: str,
        sender: str | None,
        occurred_at: datetime | str | None,
        sms_message_sid: str | None = None,
        sms_from: str | None = None,
    ) -> dict[str, Any]:
        ext: dict[str, Any] = {}
        if sms_message_sid:
            ext["smsMessageSid"] = sms_message_sid
        if sms_from:
            ext["smsFrom"] = sms_from
        return {
            "taskId": task_id,
            "threadId": thread_id,
            "content": content,
            "channel": "SMS",
            "sender": sender or "",
            "messageId": "",
            "occurredAt": _iso(occurred_at),
            "extendedParameters": ext,
        }

    def build_whatsapp_payload(
        self,
        *,
        task_id: str,
        thread_id: str,
        content: str,
        sender: str | None,
        occurred_at: datetime | str | None,
        whatsapp_conversation_id: str | None = None,
        whatsapp_message_id: str | None = None,
    ) -> dict[str, Any]:
        ext: dict[str, Any] = {}
        if whatsapp_conversation_id:
            ext["whatsappConversationId"] = whatsapp_conversation_id
        if whatsapp_message_id:
            ext["whatsappMessageId"] = whatsapp_message_id
        return {
            "taskId": task_id,
            "threadId": thread_id,
            "content": content,
            "channel": "WhatsApp",
            "sender": sender or "",
            "messageId": "",
            "occurredAt": _iso(occurred_at),
            "extendedParameters": ext,
        }

    def build_email_payload(
        self,
        *,
        task_id: str,
        thread_id: str,
        content: str,
        sender: str | None,
        occurred_at: datetime | str | None,
        subject: str | None = None,
        gmail_thread_id: str | None = None,
        gmail_message_id: str | None = None,
        extra_extended: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ext: dict[str, Any] = {}
        if gmail_thread_id:
            ext["gmailThreadId"] = gmail_thread_id
        if gmail_message_id:
            ext["gmailMessageId"] = gmail_message_id
        if extra_extended:
            ext.update({k: v for k, v in extra_extended.items() if v is not None})
        return {
            "taskId": task_id,
            "threadId": thread_id,
            "content": content,
            "channel": "Email",
            "sender": sender or "",
            "subject": subject or "",
            "messageId": "",
            "occurredAt": _iso(occurred_at),
            "extendedParameters": ext,
        }


def _iso(value: datetime | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
