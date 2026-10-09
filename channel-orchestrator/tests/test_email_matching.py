from __future__ import annotations

import base64
from unittest.mock import MagicMock

from channel_orchestrator.cache import OutboundCache
from channel_orchestrator.config import settings
from channel_orchestrator.email_classify import classify_inbound_email
from channel_orchestrator.email_util import normalize_subject, subject_matches_outbound
from channel_orchestrator.gmail_client import _extract_failed_recipients, _extract_plain_body
from channel_orchestrator.inbound import handle_inbound_email

NELLIE_SUBJECT = "Nellie’s next purchase — following Newtopia"
ROTTEN_SUBJECT = "Rotten (candy) FridgeChannel sample is live, great meeting you at NACS Show 2026"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _cache(tmp_path, monkeypatch) -> OutboundCache:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    return OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")


def _ready(cache: OutboundCache, email: str, task: str, *, subject: str | None, thread: str) -> None:
    extra = {"gmail_thread_id": thread, "gmailThreadId": thread}
    if subject is not None:
        extra["subject"] = subject
    cache.set_ready(
        email,
        task_page_id=task,
        thread_id=f"THR-{task}-Email",
        contact_page_id="c",
        conversation_page_id=f"conv-{task}",
        extra=extra,
    )


def _webhook() -> MagicMock:
    wh = MagicMock()
    wh.build_email_payload.side_effect = lambda **kw: {
        "taskId": kw["task_id"],
        "threadId": kw["thread_id"],
        "content": kw["content"],
        "channel": "Email",
        "subject": kw.get("subject") or "",
        "messageId": "",
        "extendedParameters": kw.get("extra_extended") or {},
    }
    wh.post_reply.return_value = {"ok": True}
    return wh


def _cold_portal(monkeypatch) -> MagicMock:
    portal = MagicMock()
    portal.post_inbound.return_value = {"ok": True, "skipped": True}
    monkeypatch.setattr(
        "channel_orchestrator.email_cold_inbound.InboundPortalClient",
        lambda: portal,
    )
    return portal


def test_normalize_subject_strips_reply_prefixes_only():
    assert normalize_subject("Automatic reply: [External] RE: Fwd: Hello there") == "hello there"
    assert normalize_subject("Report for Q3") == "report for q3"
    assert normalize_subject("Retail accounts") == "retail accounts"


def test_subject_match_allows_custom_auto_reply_prefix():
    assert subject_matches_outbound(f"I'm at NACS! Re: {ROTTEN_SUBJECT}", ROTTEN_SUBJECT)
    assert subject_matches_outbound("Re: Nellie's next purchase - following Newtopia", NELLIE_SUBJECT)
    assert not subject_matches_outbound("Re: Hello", "Hello")
    assert not subject_matches_outbound("Re: something else entirely", NELLIE_SUBJECT)


def test_classify_auto_replied_custom_subject_is_auto_reply():
    cls = classify_inbound_email(
        sender="brandon@eatrotten.com",
        subject=f"I'm at NACS! Re: {ROTTEN_SUBJECT}",
        body="Greetings! I'm at NACS hanging out at booth #C3774.",
        headers={"auto-submitted": "auto-replied", "x-autoreply": "yes", "precedence": "bulk"},
    )
    assert cls["kind"] == "auto_reply"


def test_classify_auto_generated_receipt_still_ignored():
    cls = classify_inbound_email(
        sender="notifications@vendor.com",
        subject="Your ticket #123 was received",
        body="We received your request.",
        headers={"auto-submitted": "auto-generated"},
    )
    assert cls["kind"] == "system_other"


def test_extract_plain_body_html_only_message():
    payload = {"mimeType": "text/html", "body": {"data": _b64("<div>Hi<br>Thank you,</div><div>Brandon</div>")}}
    assert _extract_plain_body(payload) == "Hi\nThank you,\nBrandon"


def test_extract_failed_recipients_from_header_dsn_and_body():
    found = _extract_failed_recipients(
        {"x-failed-recipients": "gone@acme.com"},
        "Your message wasn't delivered to other@acme.com because the address couldn't be found.",
        ["Reporting-MTA: dns; googlemail.com\nFinal-Recipient: rfc822; dsn@acme.com\nAction: failed\n"],
        exclude={"ella@fridgechannels.com"},
    )
    assert found == ["gone@acme.com", "dsn@acme.com", "other@acme.com"]


def test_extract_failed_recipients_skips_daemon_and_self():
    found = _extract_failed_recipients(
        {"x-failed-recipients": "ella@fridgechannels.com, mailer-daemon@googlemail.com"},
        "",
        [],
        exclude={"ella@fridgechannels.com"},
    )
    assert found == []


def test_auto_reply_new_thread_matches_exact_email(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _ready(cache, "brandon@eatrotten.com", "task-rotten", subject=ROTTEN_SUBJECT, thread="gt-sent")
    wh = _webhook()
    result = handle_inbound_email(
        {
            "sender": "brandon@eatrotten.com",
            "subject": f"I'm at NACS! Re: {ROTTEN_SUBJECT}",
            "body": "Greetings! I'm at NACS hanging out at booth #C3774.",
            "gmail_thread_id": "gt-new-thread",
            "gmail_message_id": "gm-auto",
            "classify": {"kind": "auto_reply", "reason": "auto", "is_human": False},
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    assert result["task_page_id"] == "task-rotten"
    ext = wh.post_reply.call_args.args[0]["extendedParameters"]
    assert ext["replyMatch"] == "email_exact"
    assert ext["autoReply"] is True


def test_colleague_new_thread_matches_by_domain_and_subject(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _ready(cache, "paula@nelliesclean.com", "task-nellie", subject=NELLIE_SUBJECT, thread="gt-sent")
    wh = _webhook()
    result = handle_inbound_email(
        {
            "sender": "Paula Grigg <paulag@nelliesclean.com>",
            "subject": f"Re: {NELLIE_SUBJECT}",
            "body": "Thanks for reaching out.",
            "gmail_thread_id": "gt-other",
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    assert result["task_page_id"] == "task-nellie"
    meta = result["reply_meta"]
    assert meta["replyMatch"] == "domain_subject"
    assert meta["proxyReply"] is True
    assert meta["outboundTo"] == "paula@nelliesclean.com"
    assert meta["replyFrom"] == "paulag@nelliesclean.com"


def test_colleague_new_thread_without_subject_match_goes_cold(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _ready(cache, "paula@nelliesclean.com", "task-nellie", subject=NELLIE_SUBJECT, thread="gt-sent")
    portal = _cold_portal(monkeypatch)
    wh = _webhook()
    result = handle_inbound_email(
        {
            "sender": "paulag@nelliesclean.com",
            "subject": "Lunch on Friday?",
            "body": "Unrelated",
            "gmail_thread_id": "gt-other",
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is False
    assert result.get("cold") is True
    wh.post_reply.assert_not_called()
    portal.post_inbound.assert_called()


def test_domain_subject_ambiguous_tasks_goes_cold(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    subject = "FridgeChannel sample for Team Ready"
    _ready(cache, "lmims@teamready.com", "task-l", subject=subject, thread="gt-l")
    _ready(cache, "dogren@teamready.com", "task-d", subject=subject, thread="gt-d")
    portal = _cold_portal(monkeypatch)
    wh = _webhook()
    result = handle_inbound_email(
        {
            "sender": "boss@teamready.com",
            "subject": f"Re: {subject}",
            "body": "Who sent this?",
            "gmail_thread_id": "gt-new",
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is False
    wh.post_reply.assert_not_called()
    portal.post_inbound.assert_called()


def test_domain_subject_never_matches_public_domain(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    subject = "FridgeChannel sample for your shop"
    _ready(cache, "owner@gmail.com", "task-g", subject=subject, thread="gt-g")
    _cold_portal(monkeypatch)
    result = handle_inbound_email(
        {
            "sender": "stranger@gmail.com",
            "subject": f"Re: {subject}",
            "body": "hi",
            "gmail_thread_id": "gt-new",
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=_webhook(),
        write_notion=False,
    )
    assert result["matched"] is False


def test_hard_bounce_new_thread_matches_failed_recipient(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _ready(cache, "gone@acme.com", "task-gone", subject="Hello Acme team", thread="gt-sent")
    notion = MagicMock()
    wh = _webhook()
    result = handle_inbound_email(
        {
            "sender": "mailer-daemon@googlemail.com",
            "subject": "Delivery Status Notification (Failure)",
            "body": "Address not found. Your message wasn't delivered to gone@acme.com",
            "gmail_thread_id": "gt-bounce-new",
            "failed_recipients": ["gone@acme.com"],
            "classify": {"kind": "hard_bounce", "reason": "Email硬退信：Mailer-Daemon 退信", "is_human": False},
        },
        notion=notion,
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result.get("bounce") is True
    assert result["matched"] is True
    assert result["event"]["matched_by"] == "failed_recipient"
    assert notion.update_task_status.call_args.args[:2] == ("task-gone", "Failed")
    assert cache.get("gone@acme.com")["state"] == "failed"
    wh.post_reply.assert_not_called()


def test_cc_bounce_in_sent_thread_does_not_fail_to_task(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _ready(cache, "tyson.koch@wis-pak.com", "task-tyson", subject="Please share with your colleague", thread="gt-sent")
    notion = MagicMock()
    result = handle_inbound_email(
        {
            "sender": "mailer-daemon@googlemail.com",
            "subject": "Delivery Status Notification (Failure)",
            "body": "Your message wasn't delivered to Zach.lastrilla@wis-pak.com",
            "gmail_thread_id": "gt-sent",
            "failed_recipients": ["zach.lastrilla@wis-pak.com"],
            "classify": {"kind": "hard_bounce", "reason": "Email硬退信", "is_human": False},
        },
        notion=notion,
        cache=cache,
        reply_webhook=_webhook(),
        write_notion=False,
    )
    assert result["matched"] is False
    notion.update_task_status.assert_not_called()
    assert cache.get("tyson.koch@wis-pak.com")["state"] == "ready"


def test_bounce_in_sent_thread_naming_to_recipient_fails_task(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _ready(cache, "gone@acme.com", "task-gone", subject="Hello Acme team", thread="gt-sent")
    notion = MagicMock()
    result = handle_inbound_email(
        {
            "sender": "mailer-daemon@googlemail.com",
            "subject": "Delivery Status Notification (Failure)",
            "body": "Your message wasn't delivered to Gone@acme.com",
            "gmail_thread_id": "gt-sent",
            "failed_recipients": ["gone@acme.com"],
            "classify": {"kind": "hard_bounce", "reason": "Email硬退信", "is_human": False},
        },
        notion=notion,
        cache=cache,
        reply_webhook=_webhook(),
        write_notion=False,
    )
    assert result["matched"] is True
    assert result["event"]["matched_by"] == "gmail_thread"
    assert notion.update_task_status.call_args.args[:2] == ("task-gone", "Failed")


def test_bounce_for_unknown_recipient_stays_unmatched(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _ready(cache, "alive@acme.com", "task-alive", subject="Hello Acme team", thread="gt-sent")
    notion = MagicMock()
    result = handle_inbound_email(
        {
            "sender": "mailer-daemon@googlemail.com",
            "subject": "Delivery Status Notification (Failure)",
            "body": "Your message wasn't delivered to nobody@elsewhere.com",
            "gmail_thread_id": "gt-bounce-new",
            "failed_recipients": ["nobody@elsewhere.com"],
            "classify": {"kind": "hard_bounce", "reason": "Email硬退信", "is_human": False},
        },
        notion=notion,
        cache=cache,
        reply_webhook=_webhook(),
        write_notion=False,
    )
    assert result["matched"] is False
    assert result["webhook"]["reason"] == "bounce_unmatched"
    notion.update_task_status.assert_not_called()
    assert cache.get("alive@acme.com")["state"] == "ready"
