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
from .inbound_portal import InboundPortalClient

log = logging.getLogger(__name__)


def handle_email_cold_inbound(
    normalized: dict[str, Any],
    *,
    portal: InboundPortalClient | None = None,
    domain_cache: Any | None = None,
) -> dict[str, Any]:
    """No Task match → POST /api/inbound for each (sender, FollowUpClientId?) pair."""
    portal = portal or InboundPortalClient()
    cache = domain_cache or get_domain_cache()
    public = parse_public_domains(settings.email_public_domains)
    internal = parse_internal_domains(settings.email_internal_domains)

    subject = str(normalized.get("subject") or "").strip() or "(no subject)"
    body = str(normalized.get("body") or normalized.get("text") or "")
    gmail_message_id = normalized.get("gmail_message_id") or normalized.get("message_id")

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

    calls: list[dict[str, Any]] = []
    for sender, fcid in posts:
        result = portal.post_inbound(
            channel="Email",
            content=body,
            sender=sender,
            subject=subject,
            followup_client_id=fcid,
        )
        calls.append(
            {
                "sender": sender,
                "FollowUpClientId": fcid,
                "result": result,
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
        "event": {
            "channel": "Email",
            "direction": "cold_inbound",
            "subject": subject,
            "external_emails": external,
            "posts": len(calls),
        },
    }
