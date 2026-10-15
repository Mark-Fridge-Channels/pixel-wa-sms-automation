from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from .cache import OutboundCache
from .channels import channel_display_name, normalize_channel
from .config import settings
from .email_classify import classify_inbound_email
from .email_suppression import EmailSuppressionList
from .email_util import (
    classify_email_reply_relation,
    normalize_email,
    parse_public_domains,
    subject_matches_outbound,
)
from .exec_log import log_inbound_event
from .email_attachments import EmailAttachmentPipeline, append_content_notices
from .notion_client import NotionClient
from .outbound import fail_email_task_from_bounce
from .phone import normalize_e164
from .reply_webhook import ReplyWebhookClient

log = logging.getLogger(__name__)


def _enrich_email_attachments(normalized: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str]:
    """Upload Gmail parts → Portal attachments; append notices into content. Never fails hard."""
    body = str(normalized.get("body") or normalized.get("text") or "")
    parts = normalized.get("attachment_parts") or []
    if not isinstance(parts, list) or not parts:
        # Still allow precomputed attachments/notices
        pre = normalized.get("attachments") if isinstance(normalized.get("attachments"), list) else []
        notices = normalized.get("attachment_notices") if isinstance(normalized.get("attachment_notices"), list) else []
        body2 = append_content_notices(body, [str(n) for n in notices])
        return list(pre), [str(n) for n in notices], body2

    mid = str(normalized.get("gmail_message_id") or normalized.get("message_id") or "")
    pipeline = EmailAttachmentPipeline()
    try:
        from .gmail_client import GmailClient

        gmail = GmailClient()
        attachments, notices = pipeline.process_gmail_parts(
            gmail=gmail,
            message_id=mid,
            parts=parts,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("email attachment enrich failed")
        attachments, notices = [], [f"附件处理异常：{e}"]
    body2 = append_content_notices(body, notices)
    normalized["attachments"] = attachments
    normalized["attachment_notices"] = notices
    normalized["body"] = body2
    return attachments, notices, body2


def _unique_task(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_task = {str(e.get("task_page_id")): e for e in entries if e.get("task_page_id")}
    return next(iter(by_task.values())) if len(by_task) == 1 else None


def _match_bounce_recipient(cache: OutboundCache, failed_recipients: Any) -> dict[str, Any] | None:
    """Bounce outside the sent thread: the address it names must be one sent Task."""
    if not isinstance(failed_recipients, list):
        return None
    hits: list[dict[str, Any]] = []
    for raw in failed_recipients:
        entry = cache.get(str(raw))
        if (
            entry
            and entry.get("state") in {OutboundCache.STATE_READY, OutboundCache.STATE_PENDING}
            and entry.get("thread_id")
        ):
            hits.append(entry)
    return _unique_task(hits)


_NOTES_TO_RE = re.compile(r"^To:\s*(\S+@\S+)", re.M)


def _match_bounce_via_notion(
    notion: Any,
    gmail_thread_id: str | None,
    failed_named: set[str],
) -> dict[str, Any] | None:
    """Cache lost the row: find the sent Task through its Conversation's gmailThreadId."""
    if not gmail_thread_id or not failed_named:
        return None
    try:
        hit = notion.find_outbound_email_by_gmail_thread(gmail_thread_id)
        if not isinstance(hit, dict) or not hit.get("task_id"):
            return None
        task_id = str(hit["task_id"])
        status = notion.get_task_status(task_id)
        if status not in {"Completed", "In Progress"}:
            log.info("bounce notion match skipped task=%s status=%s", task_id, status)
            return None
        from .notion_props import props, rich_text_plain

        resolved = notion.resolve_task_for_send(task_id)
        notes = rich_text_plain(props(notion.get_page(task_id)).get("Notes")) or ""
        candidates = {normalize_email(resolved.email)}
        candidates.update(normalize_email(m) for m in _NOTES_TO_RE.findall(notes))
        recipient = next((e for e in candidates if e and e in failed_named), None)
        if not recipient:
            log.info(
                "bounce notion match skipped task=%s: recipient %s not in %s",
                task_id,
                sorted(e for e in candidates if e),
                sorted(failed_named),
            )
            return None
        return {
            "task_page_id": task_id,
            "conversation_page_id": hit.get("conversation_id"),
            "email": recipient,
            "thread_id": resolved.thread_id,
            "gmail_thread_id": gmail_thread_id,
        }
    except Exception:  # noqa: BLE001
        log.exception("bounce notion lookup failed gmail_thread=%s", gmail_thread_id)
        return None


def _match_same_org_subject(
    cache: OutboundCache,
    party_key: str,
    subject: str,
    *,
    public_domains: set[str],
) -> dict[str, Any] | None:
    """New-thread reply from a colleague at the recipient's org, carrying our outbound subject."""
    candidates = [
        entry
        for entry in cache.list_ready_same_org(party_key, public_domains=public_domains)
        if subject_matches_outbound(subject, entry.get("subject"))
    ]
    return _unique_task(candidates)


def _inbound_status(result: dict[str, Any]) -> tuple[str, bool]:
    """Map handle_inbound_message result → (monitor status, ok)."""
    wh = result.get("webhook") if isinstance(result.get("webhook"), dict) else {}
    if result.get("bounce"):
        return "bounce", True
    if result.get("cold"):
        cold = result.get("cold_inbound") if isinstance(result.get("cold_inbound"), dict) else {}
        ok = bool(result.get("ok", True) and cold.get("ok", True))
        return "cold", ok
    if result.get("skipped"):
        return str(result.get("kind") or wh.get("reason") or "skipped"), True
    if result.get("matched"):
        if wh.get("skipped"):
            return str(wh.get("reason") or "matched_skipped"), True
        if wh and not wh.get("ok", True):
            return "webhook_failed", False
        return "matched", bool(result.get("ok", True))
    return str(wh.get("reason") or "unmatched"), True


def _log_inbound_result(
    result: dict[str, Any],
    *,
    channel: str,
    body: str = "",
    sender: str | None = None,
) -> dict[str, Any]:
    status, ok = _inbound_status(result)
    wh = result.get("webhook") if isinstance(result.get("webhook"), dict) else {}
    reason = wh.get("reason") or wh.get("error")
    if not reason and isinstance(result.get("event"), dict):
        reason = result["event"].get("reason")
    log_inbound_event(
        channel=channel,
        status=status,
        sender=sender,
        task_id=result.get("task_page_id"),
        body=body,
        reason=str(reason) if reason else None,
        matched=bool(result.get("matched")),
        ok=ok,
    )
    return result


def handle_inbound_message(
    normalized: dict[str, Any],
    *,
    channel: str = "SMS",
    notion: NotionClient | None = None,
    cache: OutboundCache | None = None,
    reply_webhook: ReplyWebhookClient | None = None,
    write_notion: bool = True,
    suppression: EmailSuppressionList | None = None,
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
    email_attachments: list[dict[str, Any]] = []
    email_attachment_notices: list[str] = []
    try:
        interaction_at = (
            datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            if received_at
            else datetime.now(timezone.utc)
        )
    except ValueError:
        interaction_at = datetime.now(timezone.utc)

    def done(result: dict[str, Any], *, preview: str | None = None, sender: str | None = None) -> dict[str, Any]:
        return _log_inbound_result(
            result,
            channel=ch,
            body=preview if preview is not None else body,
            sender=sender if sender is not None else (normalize_email(sender_raw) if ch == "EMAIL" else normalize_e164(sender_raw)) or str(sender_raw or "") or None,
        )

    # Email: bounce and empty system receipts stop here.
    # Auto-replies continue into the same listen path as human replies.
    email_kind = "human"
    if ch == "EMAIL":
        cls = normalized.get("classify") or classify_inbound_email(
            sender=normalize_email(sender_raw) or str(sender_raw or ""),
            subject=subject,
            body=body,
        )
        kind = cls.get("kind") or "human"
        email_kind = kind
        if kind == "soft_bounce":
            # Temporary failure (delay, mailbox full, …): Gmail keeps retrying; Task and cache stay as-is.
            failed = list(normalized.get("failed_recipients") or [])
            log.info(
                "email soft bounce ignored gmail_thread=%s failed_recipients=%s reason=%s",
                gmail_thread_id,
                failed,
                cls.get("reason"),
            )
            return done(
                {
                    "ok": True,
                    "matched": False,
                    "skipped": True,
                    "bounce": True,
                    "kind": kind,
                    "webhook": {"ok": True, "skipped": True, "reason": "soft_bounce_ignored"},
                    "event": {"kind": kind, "reason": cls.get("reason"), "failed_recipients": failed},
                },
                preview=subject or body,
            )
        if kind == "hard_bounce":
            failed_named = {
                e for e in (normalize_email(str(x)) for x in normalized.get("failed_recipients") or []) if e
            }
            matched = cache.get_ready_by_gmail_thread(gmail_thread_id)
            bounce_matched_by = "gmail_thread" if matched else ""
            if matched and failed_named and normalize_email(matched.get("email")) not in failed_named:
                # Same thread, but the DSN names a Cc / other address — the To recipient was delivered.
                matched = None
                bounce_matched_by = ""
            if not matched:
                matched = _match_bounce_recipient(cache, normalized.get("failed_recipients"))
                bounce_matched_by = "failed_recipient" if matched else ""
            if not matched:
                matched = _match_bounce_via_notion(notion, gmail_thread_id, failed_named)
                bounce_matched_by = "notion_thread" if matched else ""
            if failed_named:
                store = suppression or EmailSuppressionList()
                for addr in sorted(failed_named):
                    store.add(
                        addr,
                        reason=cls.get("reason") or kind,
                        kind=kind,
                        task_id=(matched or {}).get("task_page_id"),
                        gmail_thread_ids=[gmail_thread_id, (matched or {}).get("gmail_thread_id")],
                    )
                log.warning("email hard bounce suppressed addresses=%s", sorted(failed_named))
            if matched:
                bounce_result = fail_email_task_from_bounce(
                    cache_entry=matched,
                    reason=cls.get("reason") or kind,
                    kind=kind,
                    notion=notion,
                    cache=cache,
                )
                return done(
                    {
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
                            "matched_by": bounce_matched_by,
                            "failed_recipient": matched.get("email"),
                        },
                    },
                    preview=subject or body,
                )
            failed = list(normalized.get("failed_recipients") or [])
            log.info(
                "email bounce unmatched kind=%s gmail_thread=%s failed_recipients=%s reason=%s",
                kind,
                gmail_thread_id,
                failed,
                cls.get("reason"),
            )
            return done(
                {
                    "ok": True,
                    "matched": False,
                    "bounce": True,
                    "kind": kind,
                    "webhook": {"ok": True, "skipped": True, "reason": "bounce_unmatched"},
                    "event": {
                        "kind": kind,
                        "reason": cls.get("reason"),
                        "failed_recipients": failed,
                    },
                },
                preview=subject or body,
            )
        if kind == "system_other":
            log.info("email non-human ignored kind=%s reason=%s", kind, cls.get("reason"))
            return done(
                {
                    "ok": True,
                    "matched": False,
                    "skipped": True,
                    "kind": kind,
                    "webhook": {"ok": True, "skipped": True, "reason": kind},
                    "event": {"kind": kind, "reason": cls.get("reason")},
                },
                preview=subject or body,
            )
        if kind == "auto_reply":
            log.info(
                "email auto-reply inbound sender=%s subject=%s",
                normalize_email(sender_raw) or sender_raw,
                subject,
            )

    party_key: str | None
    matched: dict[str, Any] | None = None
    reply_meta: dict[str, Any] = {}
    if ch == "EMAIL":
        party_key = normalize_email(sender_raw)
        pubs = parse_public_domains(settings.email_public_domains)
        matched = cache.get_ready_by_gmail_thread(gmail_thread_id)
        matched_by = "gmail_thread" if matched else ""
        if not matched and party_key:
            matched = cache.get_ready_for_reply(party_key)
            matched_by = "email_exact" if matched else ""
        if not matched and party_key:
            matched = _match_same_org_subject(cache, party_key, subject, public_domains=pubs)
            matched_by = "domain_subject" if matched else ""
        if matched:
            outbound_to = matched.get("email")
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
                return done(
                    {
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
                    },
                    preview=subject or body,
                    sender=party_key,
                )
    else:
        party_key = normalize_e164(sender_raw)
        matched = cache.get_ready_for_reply(party_key) if party_key else None

    task_id = matched.get("task_page_id") if matched else None
    thread_id = matched.get("thread_id") if matched else None
    contact_id = matched.get("contact_page_id") if matched else None
    matched_flag = bool(task_id and thread_id)
    media_url = normalized.get("media_url") or normalized.get("mediaUrl")
    media_type = normalized.get("media_type") or normalized.get("mediaType")
    media_content_type = normalized.get("media_content_type") or normalized.get("mediaContentType")
    media_filename = normalized.get("media_filename") or normalized.get("mediaFilename")
    if not body and media_type:
        body = f"[{media_type}]"

    if ch == "EMAIL":
        # After bounce / system-receipt skip: upload attachments for Task replies or cold inbound.
        email_attachments, email_attachment_notices, body = _enrich_email_attachments(normalized)

    label = channel_display_name(ch)
    # Inbound listen: log + Portal callback only. Do NOT create Notion Conversation
    # rows here — Portal owns Conversation writes after /api/replies.
    if write_notion:
        log.debug(
            "write_notion ignored for inbound conversation create channel=%s "
            "(Portal owns Conversation inserts)",
            label,
        )

    webhook_result: dict[str, Any]
    if matched_flag and task_id and thread_id and (body or media_url or email_attachments):
        if ch == "WHATSAPP":
            payload = reply_webhook.build_whatsapp_payload(
                task_id=task_id,
                thread_id=thread_id,
                content=body,
                sender=party_key,
                occurred_at=interaction_at,
                whatsapp_conversation_id=f"whatsapp:{party_key}" if party_key else None,
                media_url=str(media_url) if media_url else None,
                media_type=str(media_type) if media_type else None,
                media_content_type=str(media_content_type) if media_content_type else None,
                media_filename=str(media_filename) if media_filename else None,
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
                    "autoReply": True if email_kind == "auto_reply" else None,
                },
                attachments=email_attachments or None,
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
    elif ch == "EMAIL" and (body or email_attachments or email_attachment_notices):
        # No Task ready → cold inbound (POST /api/inbound); ignore Gmail thread cache
        from .email_cold_inbound import handle_email_cold_inbound

        cold = handle_email_cold_inbound(normalized)
        return done(
            {
                "ok": cold.get("ok", True),
                "matched": False,
                "cold": True,
                "task_page_id": None,
                "thread_id": None,
                "conversation_page_id": None,
                "reply_meta": None,
                "webhook": {"ok": True, "skipped": True, "reason": "cold_inbound"},
                "cold_inbound": cold,
                "event": cold.get("event")
                or {
                    "channel": "Email",
                    "direction": "cold_inbound",
                    "matched": False,
                },
            },
            preview=subject or body,
            sender=party_key,
        )
    else:
        log.info(
            "inbound unmatched or incomplete channel=%s party=%s has_body=%s has_media=%s",
            label,
            party_key or sender_raw,
            bool(body),
            bool(media_url),
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
        "conversation_page_id": None,
        "from": party_key or sender_raw,
        "body": body,
        "subject": subject or None,
        "gmail_thread_id": gmail_thread_id,
        "reply_meta": reply_meta or None,
        "received_at": interaction_at.isoformat(),
        "saily_phone": settings.saily_phone_e164 or None,
        "webhook": webhook_result,
    }

    return done(
        {
            "ok": True,
            "matched": matched_flag,
            "task_page_id": task_id,
            "thread_id": thread_id,
            "conversation_page_id": None,
            "reply_meta": reply_meta or None,
            "webhook": webhook_result,
            "event": event,
        },
        preview=subject or body,
        sender=party_key or (str(sender_raw) if sender_raw else None),
    )


def handle_inbound_sms(*args: Any, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("channel", "SMS")
    return handle_inbound_message(*args, **kwargs)


def handle_inbound_whatsapp(normalized: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return handle_inbound_message(normalized, channel="WHATSAPP", **kwargs)


def handle_inbound_email(normalized: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return handle_inbound_message(normalized, channel="EMAIL", **kwargs)


def ingest_gmail_messages(
    message_ids: list[str],
    *,
    force: bool = False,
    gmail: Any | None = None,
    notion: NotionClient | None = None,
    cache: OutboundCache | None = None,
    reply_webhook: ReplyWebhookClient | None = None,
) -> list[dict[str, Any]]:
    """Parse specific Gmail messages and run the email listen path.

    History poll will not see a message again after the cursor moves on.
    ``force`` clears the dedupe key left by an earlier skip (such as the
    old auto-reply ignore) and ingests once more.
    """
    from .gmail_client import GmailClient
    from .inbound_dedupe import already_seen, forget, seen_or_mark

    gmail = gmail or GmailClient()
    email_cache = cache or OutboundCache(channel="EMAIL")
    results: list[dict[str, Any]] = []
    for mid in message_ids:
        mid = str(mid or "").strip()
        if not mid:
            continue
        if already_seen("EMAIL", mid) and not force:
            log.info("skip duplicate gmail ingest id=%s", mid)
            results.append(
                {
                    "ok": True,
                    "matched": False,
                    "skipped": True,
                    "reason": "duplicate_inbound",
                    "gmail_message_id": mid,
                }
            )
            continue
        if force:
            forget("EMAIL", mid)
        try:
            parsed = gmail.parse_inbound_message(mid)
        except Exception:  # noqa: BLE001
            log.exception("failed to parse gmail message %s", mid)
            results.append(
                {
                    "ok": False,
                    "matched": False,
                    "gmail_message_id": mid,
                    "error": "parse_failed",
                }
            )
            continue
        if not parsed:
            results.append(
                {
                    "ok": False,
                    "matched": False,
                    "gmail_message_id": mid,
                    "error": "empty_message",
                }
            )
            continue
        result = handle_inbound_email(
            parsed,
            notion=notion,
            cache=email_cache,
            reply_webhook=reply_webhook,
        )
        result["gmail_message_id"] = mid
        webhook = result.get("webhook") if isinstance(result.get("webhook"), dict) else {}
        ingested = bool(result.get("matched") and webhook.get("ok") and not webhook.get("skipped"))
        ingested = ingested or bool(result.get("cold") and result.get("ok"))
        if ingested:
            seen_or_mark(
                "EMAIL",
                mid,
                body=str(parsed.get("body") or ""),
                sender=str(parsed.get("sender") or ""),
            )
        results.append(result)
    return results


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
