"""Cold inbound: no Task match → one POST /api/inbound using the real From/To/Cc headers."""

from __future__ import annotations

import logging
from typing import Any

from .client_domain_cache import get_domain_cache
from .config import settings
from .email_util import (
    email_domain,
    extract_emails_from_header_value,
    is_public_domain,
    normalize_email,
    parse_internal_domains,
    parse_public_domains,
)
from .inbound_portal import InboundPortalClient, parse_inbound_response_conversation_id
from .notion_client import NotionClient

log = logging.getLogger(__name__)


def _address_list(*sources: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for source in sources:
        values: list[str] = []
        if isinstance(source, list):
            values = [str(x) for x in source]
        elif source:
            values = extract_emails_from_header_value(str(source))
        for raw in values:
            email = normalize_email(raw)
            if email and email not in seen:
                seen.add(email)
                out.append(email)
    return out


def _join_emails(emails: list[str]) -> str | None:
    return ",".join(emails) if emails else None


def handle_email_cold_inbound(
    normalized: dict[str, Any],
    *,
    portal: InboundPortalClient | None = None,
    domain_cache: Any | None = None,
    notion: NotionClient | None = None,
) -> dict[str, Any]:
    """No Task match → one POST /api/inbound for the message From address.

    Always attach gmailThreadId/gmailMessageId so later outbound can reply in-thread.
    recipient/cc mirror the email To/Cc headers (including our mailbox when CC'd).
    """
    portal = portal or InboundPortalClient()
    cache = domain_cache or get_domain_cache()
    public = parse_public_domains(settings.email_public_domains)
    internal = parse_internal_domains(settings.email_internal_domains)

    subject = str(normalized.get("subject") or "").strip() or "(no subject)"
    body = str(normalized.get("body") or normalized.get("text") or "")
    gmail_message_id = normalized.get("gmail_message_id") or normalized.get("message_id")
    gmail_thread_id = normalized.get("gmail_thread_id") or normalized.get("gmailThreadId")
    attachments = (
        list(normalized.get("attachments") or [])
        if isinstance(normalized.get("attachments"), list)
        else []
    )

    sender = normalize_email(normalized.get("sender")) or normalize_email(
        (extract_emails_from_header_value(normalized.get("from_raw")) or [None])[0]
    )
    if not sender:
        log.info("cold inbound: missing From gmail_id=%s", gmail_message_id)
        return {
            "ok": True,
            "matched": False,
            "cold": True,
            "skipped": True,
            "reason": "no_from_address",
            "calls": [],
        }

    sender_domain = email_domain(sender)
    if sender_domain and (
        sender_domain in internal or any(sender_domain.endswith("." + i) for i in internal)
    ):
        log.info("cold inbound: skip internal From=%s gmail_id=%s", sender, gmail_message_id)
        return {
            "ok": True,
            "matched": False,
            "cold": True,
            "skipped": True,
            "reason": "internal_from",
            "calls": [],
        }

    to_emails = _address_list(normalized.get("to_emails"), normalized.get("to_raw"))
    cc_emails = _address_list(normalized.get("cc_emails"), normalized.get("cc_raw"))
    recipient = _join_emails(to_emails)
    cc = _join_emails(cc_emails)

    extended: dict[str, Any] = {}
    if gmail_thread_id:
        extended["gmailThreadId"] = str(gmail_thread_id)
    if gmail_message_id:
        extended["gmailMessageId"] = str(gmail_message_id)

    # Brand hint from From domain only (one call per message).
    followup_client_id: str | None = None
    if sender_domain and not is_public_domain(sender_domain, public):
        hits = cache.lookup(sender_domain)
        if len(hits) == 1:
            followup_client_id = hits[0]
        elif len(hits) > 1:
            log.info(
                "cold inbound: multiple clients for domain=%s; omit FollowUpClientId sender=%s",
                sender_domain,
                sender,
            )

    result = portal.post_inbound(
        channel="Email",
        content=body,
        sender=sender,
        subject=subject,
        recipient=recipient,
        cc=cc,
        followup_client_id=followup_client_id,
        extended_parameters=extended or None,
        attachments=attachments or None,
    )
    conv_id = parse_inbound_response_conversation_id(result)
    ext_write: dict[str, Any] | None = None
    notion_client = notion
    if result.get("ok") and conv_id and extended and settings.notion_token:
        try:
            notion_client = notion_client or NotionClient()
            notion_client.update_conversation_extended_parameters(conv_id, extended)
            ext_write = {"ok": True, "conversation_id": conv_id}
        except Exception as e:  # noqa: BLE001
            log.exception(
                "failed to write gmailThreadId on cold inbound conversation=%s",
                conv_id,
            )
            ext_write = {"ok": False, "error": str(e), "conversation_id": conv_id}
    elif result.get("ok") and extended and not conv_id:
        log.warning(
            "cold inbound ok but no conversationId; cannot patch Extended Parameters "
            "sender=%s gmail_thread=%s",
            sender,
            gmail_thread_id,
        )
        ext_write = {"ok": False, "reason": "no_conversation_id"}

    call = {
        "sender": sender,
        "recipient": recipient,
        "cc": cc,
        "FollowUpClientId": followup_client_id,
        "conversation_id": conv_id,
        "result": result,
        "extended_write": ext_write,
    }
    return {
        "ok": bool(result.get("ok")),
        "matched": False,
        "cold": True,
        "external_emails": [sender],
        "calls": [call],
        "gmail_message_id": gmail_message_id,
        "gmail_thread_id": gmail_thread_id,
        "event": {
            "channel": "Email",
            "direction": "cold_inbound",
            "subject": subject,
            "sender": sender,
            "recipient": recipient,
            "cc": cc,
            "posts": 1,
            "gmail_thread_id": gmail_thread_id,
            "gmail_message_id": gmail_message_id,
        },
    }
