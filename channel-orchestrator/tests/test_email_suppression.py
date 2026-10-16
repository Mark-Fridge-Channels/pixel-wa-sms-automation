from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import MagicMock

from channel_orchestrator.cache import OutboundCache
from channel_orchestrator.config import settings
from channel_orchestrator.email_classify import classify_inbound_email
from channel_orchestrator.email_suppression import EmailSuppressionList
from channel_orchestrator.inbound import handle_inbound_email
from channel_orchestrator.notion_client import NotionClient, ResolvedTask
from channel_orchestrator.notion_props import NOTES_MAX_CHARS, append_note_entry, latest_note_entry
from channel_orchestrator.outbound import execute_resolved_email, execute_task
from channel_orchestrator.send_safety import conversation_already_sent, retry_blocked_reason, uncertain_notes


def _setup(tmp_path, monkeypatch) -> OutboundCache:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    monkeypatch.setattr(settings, "gmail_user", "mark@fridgechannels.com")
    return OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")


def _resolved(email: str, *, cc: list[str] | None = None, gmail_thread: str | None = None) -> ResolvedTask:
    return ResolvedTask(
        task_id="task-shaw",
        title="Grinds follow up",
        priority="P0",
        scheduled_at="2026-10-15",
        status="Pending",
        contact_id="c1",
        keyperson_id="k1",
        phone_e164=None,
        conversation_id="conv-shaw",
        content="Body",
        thread_id="THR-shaw-Email",
        channel="EMAIL",
        email=email,
        subject="Grinds follow up",
        cc=cc or [],
        extended_parameters={"gmailThreadId": gmail_thread} if gmail_thread else {},
    )


def _gmail() -> MagicMock:
    gmail = MagicMock()
    gmail.send_new_or_reply.return_value = {"gmail_thread_id": "gt-new", "gmail_message_id": "gm-new"}
    return gmail


def _bounce(failed: str, thread: str) -> dict:
    return {
        "sender": "mailer-daemon@googlemail.com",
        "subject": "Delivery Status Notification (Failure)",
        "body": f"Address not found. Your message wasn't delivered to {failed}",
        "gmail_thread_id": thread,
        "failed_recipients": [failed],
        "classify": {"kind": "hard_bounce", "reason": "Email硬退信：地址不存在", "is_human": False},
    }


def test_hard_bounce_suppresses_and_fails_task(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    cache.set_ready(
        "jshaw@getgrinds.con",
        task_page_id="task-shaw",
        thread_id="THR-shaw-Email",
        contact_page_id="c1",
        conversation_page_id="conv-shaw",
        extra={"gmail_thread_id": "gt-sent"},
    )
    notion = MagicMock()
    result = handle_inbound_email(
        _bounce("jshaw@getgrinds.con", "gt-sent"), notion=notion, cache=cache, reply_webhook=MagicMock(), write_notion=False
    )
    assert result["matched"] is True
    assert notion.update_task_status.call_args.args[:2] == ("task-shaw", "Failed")
    entry = EmailSuppressionList().get("JShaw@GetGrinds.con")
    assert entry and entry["task_ids"] == ["task-shaw"]
    assert "gt-sent" in entry["gmail_thread_ids"]


def test_unmatched_hard_bounce_still_suppresses(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    result = handle_inbound_email(
        _bounce("nobody@elsewhere.com", "gt-x"), notion=MagicMock(), cache=cache, reply_webhook=MagicMock(), write_notion=False
    )
    assert result["matched"] is False
    assert EmailSuppressionList().get("nobody@elsewhere.com")


def test_soft_bounce_is_ignored(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    cache.set_ready(
        "busy@acme.com",
        task_page_id="task-busy",
        thread_id="THR-busy-Email",
        contact_page_id="c",
        conversation_page_id="conv-busy",
        extra={"gmail_thread_id": "gt-x"},
    )
    msg = _bounce("busy@acme.com", "gt-x")
    msg["classify"] = {"kind": "soft_bounce", "reason": "Email软退信", "is_human": False}
    notion = MagicMock()
    result = handle_inbound_email(msg, notion=notion, cache=cache, reply_webhook=MagicMock(), write_notion=False)
    assert result["skipped"] is True
    assert result["webhook"]["reason"] == "soft_bounce_ignored"
    notion.update_task_status.assert_not_called()
    notion.update_conversation_outbound.assert_not_called()
    assert cache.get("busy@acme.com")["state"] == "ready"
    assert EmailSuppressionList().get("busy@acme.com") is None


def test_task_failed_notes_append_not_overwrite(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    client = NotionClient.__new__(NotionClient)
    client.get_page = MagicMock(
        return_value={"properties": {"Notes": {"rich_text": [{"plain_text": "To: jshaw@getgrinds.con\n展会跟进"}]}}}
    )
    client._request = MagicMock(return_value={})
    client.update_task_status("task-shaw", "Failed", notes="Email硬退信：地址不存在")
    sent = client._request.call_args.kwargs["json"]["properties"]["Notes"]["rich_text"]
    text = "".join(c["text"]["content"] for c in sent)
    assert text.startswith("To: jshaw@getgrinds.con\n展会跟进")
    assert "【Failed · " in text and text.endswith("Email硬退信：地址不存在")


def test_append_note_entry_dedupes_and_trims():
    at = datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)
    once = append_note_entry("原始内容", "Failed", "退信", at, "America/New_York")
    assert append_note_entry(once, "Failed", "退信", at, "America/New_York") == once
    big = once
    for i in range(400):
        big = append_note_entry(big, "Failed", f"第{i}次" + "x" * 100, at, "America/New_York")
    assert len(big) <= NOTES_MAX_CHARS
    assert big.startswith("原始内容")
    assert latest_note_entry(big).startswith("第399次")


def test_uncertain_before_failure_no_longer_blocks_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    at = datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)
    notes = append_note_entry(uncertain_notes("发送超时"), "Failed", "Email硬退信", at, "America/New_York")
    notion = MagicMock()
    notion.get_page.return_value = {
        "properties": {
            "Notes": {"rich_text": [{"plain_text": notes}]},
            "Interaction At": {"type": "date", "date": None},
        }
    }
    assert retry_blocked_reason(notion=notion, task_id="t", conversation_id=None) is None


def test_pending_cache_row_matches_bounce(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    cache.set_pending(
        "gone@acme.com",
        task_page_id="task-gone",
        thread_id="THR-gone-Email",
        contact_page_id="c",
        conversation_page_id="conv-gone",
    )
    notion = MagicMock()
    result = handle_inbound_email(
        _bounce("gone@acme.com", "gt-bounce"), notion=notion, cache=cache, reply_webhook=MagicMock(), write_notion=False
    )
    assert result["matched"] is True
    assert notion.update_task_status.call_args.args[:2] == ("task-gone", "Failed")


def test_notion_thread_fallback_when_cache_lost(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    notion = MagicMock()
    notion.find_outbound_email_by_gmail_thread.return_value = {"conversation_id": "conv-shaw", "task_id": "task-shaw"}
    notion.get_task_status.return_value = "Completed"
    notion.resolve_task_for_send.return_value = _resolved("jshaw@getgrinds.con")
    notion.get_page.return_value = {"properties": {"Notes": {"type": "rich_text", "rich_text": []}}}
    result = handle_inbound_email(
        _bounce("jshaw@getgrinds.con", "gt-sent"), notion=notion, cache=cache, reply_webhook=MagicMock(), write_notion=False
    )
    assert result["matched"] is True
    assert result["event"]["matched_by"] == "notion_thread"
    assert notion.update_task_status.call_args.args[:2] == ("task-shaw", "Failed")
    assert notion.update_conversation_outbound.call_args.args[0] == "conv-shaw"


def test_notion_fallback_rejects_other_recipient(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    notion = MagicMock()
    notion.find_outbound_email_by_gmail_thread.return_value = {"conversation_id": "conv-shaw", "task_id": "task-shaw"}
    notion.get_task_status.return_value = "Completed"
    notion.resolve_task_for_send.return_value = _resolved("boss@getgrinds.com")
    notion.get_page.return_value = {"properties": {"Notes": {"type": "rich_text", "rich_text": []}}}
    result = handle_inbound_email(
        _bounce("cc-gone@getgrinds.com", "gt-sent"), notion=notion, cache=cache, reply_webhook=MagicMock(), write_notion=False
    )
    assert result["matched"] is False
    notion.update_task_status.assert_not_called()


def test_send_blocked_for_suppressed_address(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    EmailSuppressionList().add("jshaw@getgrinds.con", reason="Email硬退信：地址不存在")
    notion, gmail = MagicMock(), _gmail()
    result = execute_resolved_email(_resolved("jshaw@getgrinds.con"), notion=notion, gmail=gmail, cache=cache)
    assert result["ok"] is False and result["suppressed"] is True
    gmail.send_new_or_reply.assert_not_called()
    args, kwargs = notion.update_task_status.call_args
    assert args[:2] == ("task-shaw", "Failed")
    assert "jshaw@getgrinds.con" in kwargs["notes"] and "重新找邮箱" in kwargs["notes"]


def test_corrected_address_sends_in_new_thread(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    EmailSuppressionList().add("jshaw@getgrinds.con", reason="硬退信", gmail_thread_ids=["gt-sent"])
    gmail = _gmail()
    result = execute_resolved_email(
        _resolved("jshaw@getgrinds.com", gmail_thread="gt-sent"), notion=MagicMock(), gmail=gmail, cache=cache
    )
    assert result["ok"] is True
    kwargs = gmail.send_new_or_reply.call_args.kwargs
    assert kwargs["to"] == "jshaw@getgrinds.com"
    assert kwargs["gmail_thread_id"] is None


def test_force_does_not_bypass_suppression(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    EmailSuppressionList().add("jshaw@getgrinds.con", reason="硬退信")
    notion = MagicMock()
    notion.get_task_status.return_value = "Pending"
    notion.resolve_task_for_send.return_value = _resolved("jshaw@getgrinds.con")
    gmail = _gmail()
    monkeypatch.setattr("channel_orchestrator.gmail_client.GmailClient", lambda: gmail)
    result = execute_task("task-shaw", channel="EMAIL", notion=notion, cache=cache, force=True)
    assert result["suppressed"] is True
    gmail.send_new_or_reply.assert_not_called()


def test_suppressed_cc_is_dropped_with_warning(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    EmailSuppressionList().add("gone@getgrinds.com", reason="硬退信")
    gmail = _gmail()
    result = execute_resolved_email(
        _resolved("jshaw@getgrinds.com", cc=["Gone@getgrinds.com", "ok@getgrinds.com"]),
        notion=MagicMock(),
        gmail=gmail,
        cache=cache,
    )
    assert result["ok"] is True
    assert gmail.send_new_or_reply.call_args.kwargs["cc"] == ["ok@getgrinds.com"]
    assert any("gone@getgrinds.com" in w.lower() for w in result["attachment_warnings"])


def test_failed_conversation_is_not_counted_as_sent():
    notion = MagicMock()
    notion.get_page.return_value = {
        "properties": {
            "Interaction At": {"type": "date", "date": {"start": "2026-10-14T12:00:00.000Z"}},
            "Notes": {"type": "rich_text", "rich_text": [{"plain_text": "发送失败。"}]},
        }
    }
    assert conversation_already_sent(notion, "conv-shaw") is False


def test_delay_dsn_is_soft_bounce():
    cls = classify_inbound_email(
        sender="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Delay)",
        body="Message not delivered yet. Gmail will retry for 46 more hours.",
        headers={"auto-submitted": "auto-replied"},
    )
    assert cls["kind"] == "soft_bounce"


def test_reply_notion_thread_fallback_passes_to_and_cc(tmp_path, monkeypatch):
    """Cache miss: recover Task via Conversation.gmailThreadId and send header To/Cc."""
    cache = _setup(tmp_path, monkeypatch)
    notion = MagicMock()
    notion.find_outbound_email_by_gmail_thread.return_value = {
        "conversation_id": "conv-out",
        "task_id": "task-shaw",
        "thread_id": "THR-shaw-Email",
        "contact_page_id": "c1",
    }
    notion.get_task_status.return_value = "Completed"
    notion.resolve_task_for_send.return_value = _resolved("jack@puregreen.com")
    wh = MagicMock()
    wh.build_email_payload.side_effect = lambda **kw: kw
    wh.post_reply.return_value = {"ok": True}
    result = handle_inbound_email(
        {
            "sender": "carol@puregreen.com",
            "subject": "Re: Jack — Carol brought this back from NACS",
            "body": "Thanks for the follow-up Ella!",
            "gmail_thread_id": "1a13a85b260d09fa",
            "to_emails": ["jack@puregreen.com"],
            "cc_emails": ["ella@fridgechannels.com"],
        },
        notion=notion,
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    kwargs = wh.build_email_payload.call_args.kwargs
    assert kwargs["task_id"] == "task-shaw"
    assert kwargs["sender"] == "carol@puregreen.com"
    assert kwargs["recipient"] == "jack@puregreen.com"
    assert kwargs["cc"] == "ella@fridgechannels.com"
    wh.post_reply.assert_called_once()


def test_cache_parallel_instances_do_not_lose_rows(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    path = tmp_path / "email_cache.json"

    def write(i: int) -> None:
        OutboundCache(channel="EMAIL", path=path).set_ready(
            f"user{i}@acme.com",
            task_page_id=f"task-{i}",
            thread_id=f"THR-{i}",
            contact_page_id="c",
            conversation_page_id=f"conv-{i}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(24)))
    cache = OutboundCache(channel="EMAIL", path=path)
    assert all(cache.get(f"user{i}@acme.com") for i in range(24))
