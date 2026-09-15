from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .cache import OutboundCache
from .channels import channel_display_name, normalize_channel
from .config import settings
from .email_classify import classify_inbound_email
from .email_util import (
    classify_email_reply_relation,
    normalize_email,
    parse_public_domains,
)
from .notion_client import NotionClient
from .outbound import fail_email_task_from_bounce
from .phone import normalize_e164
from .reply_webhook import ReplyWebhookClient

log = logging.getLogger(__name__)


def handle_inbound_message(
    normalized: dict[str, Any],
    *,
    channel: str = "SMS",
    notion: NotionClient | None = None,
    cache: OutboundCache | None = None,
    reply_webhook: ReplyWebhookClient | None = None,
    write_notion: bool = True,
) -> dict[str, Any]:
    ch = normalize_channel(channel)
    notion = notion or NotionClient()
    cache = cache or OutboundCache(channel=ch)
    reply_webhook = reply_webhook or ReplyWebhookClient()

    sender_raw = normalized.get("sender") or normalized.get("from") or normalized.get("title")
    body = normalized.get("body") or normalized.get("text") or ""
    provider_message_id = normalized.get("message_id") or normalized.get("gmail_message_id")
    gmail_thread_id = normalized.get("gmail_thread_id") or normalized.get("gmailThreadId")
    subject = normalized.get("subject") or ""
    received_at = normalized.get("received_at")
    try:
        interaction_at = (
            datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            if received_at
            else datetime.now(timezone.utc)
        )
    except ValueError:
        interaction_at = datetime.now(timezone.utc)

    # Email: classify bounce / auto-reply before treating as human reply
    if ch == "EMAIL":
        cls = normalized.get("classify") or classify_inbound_email(
            sender=normalize_email(sender_raw) or str(sender_raw or ""),
            subject=subject,
            body=body,
        )
        kind = cls.get("kind") or "human"
        if kind in {"hard_bounce", "soft_bounce"}:
            matched = cache.get_ready_by_gmail_thread(gmail_thread_id)
            if not matched:
                # fallback: sometimes bounce From is mailer-daemon; try original recipient in body later
                matched = None
            if matched:
                bounce_result = fail_email_task_from_bounce(
                    cache_entry=matched,
                    reason=cls.get("reason") or kind,
                    kind=kind,
                    notion=notion,
                    cache=cache,
                )
                return {
                    "ok": True,
                    "matched": True,
                    "bounce": True,
                    "kind": kind,
                    "task_page_id": bounce_result.get("task_id"),
                    "thread_id": matched.get("thread_id"),
                    "webhook": {"ok": True, "skipped": True, "reason": "bounce_not_reply"},
                    "event": {
                        "channel": "Email",
                        "direction": "bounce",
                        "kind": kind,
                        "reason": cls.get("reason"),
                        "gmail_thread_id": gmail_thread_id,
                    },
                }
            log.info(
                "email bounce unmatched kind=%s gmail_thread=%s reason=%s",
                kind,
                gmail_thread_id,
                cls.get("reason"),
            )
            return {
                "ok": True,
                "matched": False,
                "bounce": True,
                "kind": kind,
                "webhook": {"ok": True, "skipped": True, "reason": "bounce_unmatched"},
                "event": {"kind": kind, "reason": cls.get("reason")},
            }
        if kind in {"auto_reply", "system_other"}:
            log.info("email non-human ignored kind=%s reason=%s", kind, cls.get("reason"))
            return {
                "ok": True,
                "matched": False,
                "skipped": True,
                "kind": kind,
                "webhook": {"ok": True, "skipped": True, "reason": kind},
                "event": {"kind": kind, "reason": cls.get("reason")},
            }

    party_key: str | None
    matched: dict[str, Any] | None = None
    reply_meta: dict[str, Any] = {}
    if ch == "EMAIL":
        party_key = normalize_email(sender_raw)
        matched = cache.get_ready_by_gmail_thread(gmail_thread_id)
        matched_by = "gmail_thread" if matched else ""
        if not matched and party_key:
            matched = cache.get_ready_for_reply(party_key)
            matched_by = "email_exact" if matched else ""
        if matched:
            outbound_to = matched.get("email")
            pubs = parse_public_domains(settings.email_public_domains)
            reply_meta = classify_email_reply_relation(
                outbound_to=outbound_to,
                reply_from=party_key,
                matched_by=matched_by or "gmail_thread",
                public_domains=pubs,
                allow_cross_domain_thread=settings.email_allow_cross_domain_thread_reply,
            )
            if not reply_meta.get("accept"):
                log.info(
                    "email thread matched but cross-domain rejected from=%s outbound=%s",
                    party_key,
                    outbound_to,
                )
                return {
                    "ok": True,
                    "matched": False,
                    "skipped": True,
                    "kind": "unexpected_sender_rejected",
                    "webhook": {
                        "ok": True,
                        "skipped": True,
                        "reason": "cross_domain_thread_reply_disabled",
                    },
                    "event": {
                        "channel": "Email",
                        "reply_meta": reply_meta,
                        "gmail_thread_id": gmail_thread_id,
                    },
                }
    else:
        party_key = normalize_e164(sender_raw)
        matched = cache.get_ready_for_reply(party_key) if party_key else None

    task_id = matched.get("task_page_id") if matched else None
    thread_id = matched.get("thread_id") if matched else None
    contact_id = matched.get("contact_page_id") if matched else None
    matched_flag = bool(task_id and thread_id)

    label = channel_display_name(ch)
    conversation_page = None

    if write_notion and settings.notion_token and matched_flag and thread_id:
        title = f"{label} Inbound — {party_key or sender_raw or 'unknown'}"
        ext = None
        if ch == "EMAIL":
            ext = {
                k: v
                for k, v in {
                    "gmailThreadId": gmail_thread_id,
                    "gmailMessageId": provider_message_id,
                    "outboundTo": reply_meta.get("outboundTo"),
                    "replyFrom": reply_meta.get("replyFrom") or party_key,
                    "replyMatch": reply_meta.get("replyMatch"),
                    "proxyReply": reply_meta.get("proxyReply"),
                    "sameOrgDomain": reply_meta.get("sameOrgDomain"),
                    "unexpectedSender": reply_meta.get("unexpectedSender"),
                }.items()
                if v is not None and v != ""
            }
        try:
            conversation_page = notion.create_inbound_conversation(
                content=body,
                sender=party_key or str(sender_raw or ""),
                interaction_at=interaction_at,
                thread_id=thread_id,
                message_id=str(provider_message_id) if provider_message_id else None,
                task_id=task_id,
                contact_id=contact_id,
                title=title,
                channel=ch,
                extended_parameters=ext,
                subject=subject or None,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("failed to write inbound conversation: %s", e)

    webhook_result: dict[str, Any]
    if matched_flag and task_id and thread_id and body:
        if ch == "WHATSAPP":
            payload = reply_webhook.build_whatsapp_payload(
                task_id=task_id,
                thread_id=thread_id,
                content=body,
                sender=party_key,
                occurred_at=interaction_at,
                whatsapp_conversation_id=f"whatsapp:{party_key}" if party_key else None,
            )
        elif ch == "EMAIL":
            payload = reply_webhook.build_email_payload(
                task_id=task_id,
                thread_id=thread_id,
                content=body,
                sender=party_key,
                occurred_at=interaction_at,
                subject=subject or None,
                gmail_thread_id=str(gmail_thread_id) if gmail_thread_id else None,
                gmail_message_id=str(provider_message_id) if provider_message_id else None,
                extra_extended={
                    "outboundTo": reply_meta.get("outboundTo"),
                    "replyFrom": reply_meta.get("replyFrom") or party_key,
                    "replyMatch": reply_meta.get("replyMatch"),
                    "proxyReply": reply_meta.get("proxyReply"),
                    "sameOrgDomain": reply_meta.get("sameOrgDomain"),
                    "unexpectedSender": reply_meta.get("unexpectedSender"),
                },
            )
        else:
            payload = reply_webhook.build_sms_payload(
                task_id=task_id,
                thread_id=thread_id,
                content=body,
                sender=party_key,
                occurred_at=interaction_at,
                sms_message_sid=str(provider_message_id) if provider_message_id else None,
                sms_from=party_key,
            )
        webhook_result = reply_webhook.post_reply(payload)
    else:
        log.info(
            "inbound unmatched or incomplete channel=%s party=%s has_body=%s",
            label,
            party_key or sender_raw,
            bool(body),
        )
        webhook_result = {
            "ok": True,
            "skipped": True,
            "reason": "unmatched_or_incomplete",
            "matched": False,
        }

    event = {
        "channel": label,
        "direction": "inbound",
        "matched": matched_flag,
        "task_page_id": task_id,
        "thread_id": thread_id,
        "contact_page_id": contact_id,
        "conversation_page_id": (conversation_page or {}).get("id"),
        "from": party_key or sender_raw,
        "body": body,
        "subject": subject or None,
        "gmail_thread_id": gmail_thread_id,
        "reply_meta": reply_meta or None,
        "received_at": interaction_at.isoformat(),
        "saily_phone": settings.saily_phone_e164 or None,
        "webhook": webhook_result,
    }

    return {
        "ok": True,
        "matched": matched_flag,
        "task_page_id": task_id,
        "thread_id": thread_id,
        "conversation_page_id": (conversation_page or {}).get("id"),
        "reply_meta": reply_meta or None,
        "webhook": webhook_result,
        "event": event,
    }


def handle_inbound_sms(*args: Any, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("channel", "SMS")
    return handle_inbound_message(*args, **kwargs)


def handle_inbound_whatsapp(normalized: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return handle_inbound_message(normalized, channel="WHATSAPP", **kwargs)


def handle_inbound_email(normalized: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return handle_inbound_message(normalized, channel="EMAIL", **kwargs)


def poll_gmail_inbound(
    *,
    gmail: Any | None = None,
    notion: NotionClient | None = None,
    cache: OutboundCache | None = None,
    reply_webhook: ReplyWebhookClient | None = None,
    write_notion: bool = True,
) -> list[dict[str, Any]]:
    from .gmail_client import GmailClient
    from .inbound_dedupe import seen_or_mark

    gmail = gmail or GmailClient()
    email_cache = cache or OutboundCache(channel="EMAIL")
    results = []
    for msg in gmail.poll_new_inbound():
        mid = str(msg.get("gmail_message_id") or msg.get("message_id") or "")
        if seen_or_mark(
            "EMAIL",
            mid or None,
            body=str(msg.get("body") or ""),
            sender=str(msg.get("sender") or ""),
        ):
            log.info("skip duplicate gmail inbound id=%s", mid)
            results.append(
                {
                    "ok": True,
                    "matched": False,
                    "skipped": True,
                    "reason": "duplicate_inbound",
                    "gmail_message_id": mid or None,
                }
            )
            continue
        results.append(
            handle_inbound_email(
                msg,
                notion=notion,
                cache=email_cache,
                reply_webhook=reply_webhook,
                write_notion=write_notion,
            )
        )
    return results
