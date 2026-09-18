from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


class InboundPortalClient:
    """Portal cold inbound: POST /api/inbound (no taskId)."""

    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        raw = settings.inbound_webhook_url if url is None else url
        if not raw and settings.reply_webhook_url:
            # Derive from replies URL if inbound not set
            base = settings.reply_webhook_url.rstrip("/")
            if base.endswith("/api/replies"):
                raw = base[: -len("/api/replies")] + "/api/inbound"
            else:
                raw = ""
        self.url = raw.rstrip("/") if raw else ""
        self.token = (
            settings.reply_webhook_token if token is None else token
        ) or "local-reply-ingest"

    def post_inbound(
        self,
        *,
        channel: str,
        content: str,
        sender: str,
        subject: str | None = None,
        followup_client_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.url:
            log.info("INBOUND_WEBHOOK_URL empty; skip inbound sender=%s", sender)
            return {"ok": True, "skipped": True, "reason": "no_url"}

        body: dict[str, Any] = {
            "channel": channel,
            "content": content or "",
            "sender": sender or "",
        }
        if channel == "Email":
            if not subject:
                return {
                    "ok": False,
                    "skipped": True,
                    "reason": "email_missing_object_subject",
                    "payload": body,
                }
            body["object"] = subject
        if followup_client_id:
            body["FollowUpClientId"] = followup_client_id
        # Never send taskId on cold inbound
        if "taskId" in body:
            del body["taskId"]

        if not body["sender"] or (channel != "Phone" and not body["content"]):
            return {
                "ok": False,
                "skipped": True,
                "reason": "missing_sender_or_content",
                "payload": body,
            }

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
            log.exception("POST /api/inbound failed")
            return {"ok": False, "error": str(e), "payload": body}
