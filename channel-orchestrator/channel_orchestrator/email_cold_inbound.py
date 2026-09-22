from __future__ import annotations

import logging
from typing import Any

from .client_domain_cache import get_domain_cache
from .config import settings
from .email_util import (
    collect_external_emails,
    email_domain,
    is_public_domain,
    parse_internal_domains,
    parse_public_domains,
)
from .inbound_portal import InboundPortalClient, parse_inbound_response_conversation_id
from .notion_client import NotionClient

log = logging.getLogger(__name__)


def handle_email_cold_inbound(
    normalized: dict[str, Any],
    *,
    portal: InboundPortalClient | None = None,
    domain_cache: Any | None = None,
    notion: NotionClient | None = None,
) -> dict[str, Any]:
    """No Task match → POST /api/inbound for each (sender, FollowUpClientId?) pair.

    Always attach gmailThreadId/gmailMessageId so later outbound can reply in-thread.
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

    extended: dict[str, Any] = {}
    if gmail_thread_id:
        extended["gmailThreadId"] = str(gmail_thread_id)
    if gmail_message_id:
        extended["gmailMessageId"] = str(gmail_message_id)

    external = collect_external_emails(
        from_header=normalized.get("from_raw"),
        to_header=normalized.get("to_raw"),
        cc_header=normalized.get("cc_raw"),
        from_email=normalized.get("sender"),
        to_emails=normalized.get("to_emails"),
        cc_emails=normalized.get("cc_emails"),
        internal_domains=internal,
    )
    if not external:
        log.info("cold inbound: no external addresses gmail_id=%s", gmail_message_id)
        return {
            "ok": True,
            "matched": False,
            "cold": True,
            "skipped": True,
            "reason": "no_external_addresses",
            "calls": [],
        }

    # Build unique posts: (sender, FollowUpClientId|None)
    posts: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for addr in external:
        dom = email_domain(addr)
        client_ids: list[str | None]
        if not dom or is_public_domain(dom, public):
            client_ids = [None]
        else:
            hits = cache.lookup(dom)
            client_ids = hits if hits else [None]
        for fcid in client_ids:
            key = (addr, fcid)
            if key in seen:
                continue
            seen.add(key)
            posts.append(key)

    notion_client = notion
    calls: list[dict[str, Any]] = []
    for sender, fcid in posts:
        result = portal.post_inbound(
            channel="Email",
            content=body,
            sender=sender,
            subject=subject,
            followup_client_id=fcid,
            extended_parameters=extended or None,
            attachments=attachments or None,
        )
        conv_id = parse_inbound_response_conversation_id(result)
        ext_write: dict[str, Any] | None = None
        # Belt-and-suspenders: Portal may ignore extendedParameters on /api/inbound;
        # write gmail ids onto the new Conversation so reply Tasks can resolve them.
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

        calls.append(
            {
                "sender": sender,
                "FollowUpClientId": fcid,
                "conversation_id": conv_id,
                "result": result,
                "extended_write": ext_write,
            }
        )

    any_ok = any((c.get("result") or {}).get("ok") for c in calls)
    return {
        "ok": any_ok or not calls,
        "matched": False,
        "cold": True,
        "external_emails": external,
        "calls": calls,
        "gmail_message_id": gmail_message_id,
        "gmail_thread_id": gmail_thread_id,
        "event": {
            "channel": "Email",
            "direction": "cold_inbound",
            "subject": subject,
            "external_emails": external,
            "posts": len(calls),
            "gmail_thread_id": gmail_thread_id,
            "gmail_message_id": gmail_message_id,
        },
    }
