from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from random import Random
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from channel_orchestrator.cache import OutboundCache
from channel_orchestrator.config import settings
from channel_orchestrator.inbound import handle_inbound_sms
from channel_orchestrator.notion_client import ResolvedTask
from channel_orchestrator.outbound import execute_plan_item, execute_resolved
from channel_orchestrator.phone import normalize_e164
from channel_orchestrator.scheduler import (
    DailyPlan,
    PlanItem,
    build_candidate_slots,
    due_items,
    load_plan,
    ny_today,
    parse_work_windows,
    plan_path,
    save_plan,
    schedule_tasks,
)


def _task(tid: str, priority: str = "P1", title: str = "t") -> dict:
    return {
        "id": tid,
        "properties": {
            "Follow-up Task": {"title": [{"plain_text": title}]},
            "Priority": {"type": "select", "select": {"name": priority}},
            "Task Status": {"type": "status", "status": {"name": "Pending"}},
            "Channel": {"type": "select", "select": {"name": "SMS"}},
            "Conversations": {"relation": [{"id": f"conv-{tid}"}]},
            "Follow-up Contact": {"relation": [{"id": f"contact-{tid}"}]},
        },
    }


def test_normalize_e164_us():
    assert normalize_e164("(779) 436-2345") == "+17794362345"
    assert normalize_e164("+1 779 436 2345") == "+17794362345"
    assert normalize_e164(None) is None
    assert normalize_e164("779.436.2345") == "+17794362345"
    assert normalize_e164("(C) 779-436-2345") == "+17794362345"


def test_normalize_e164_failure_reason():
    from channel_orchestrator.phone import normalize_e164_with_reason

    e164, reason = normalize_e164_with_reason("abc")
    assert e164 is None
    assert reason and "手机格式识别失败" in reason

    e1642, reason2 = normalize_e164_with_reason("12345")
    assert e1642 is None
    assert reason2 and "手机格式识别失败" in reason2


def test_work_windows_no_lunch():
    windows = parse_work_windows("09:00-12:00,14:00-18:00")
    assert len(windows) == 2
    day = date(2026, 9, 14)
    slots = build_candidate_slots(day, windows=windows, interval_seconds=90)
    tz = ZoneInfo("America/New_York")
    assert all(s.tzinfo is not None for s in slots)
    for s in slots:
        local = s.astimezone(tz)
        assert (9 <= local.hour < 12) or (14 <= local.hour < 18)
        assert not (12 <= local.hour < 14)


def test_schedule_priority_and_spacing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    monkeypatch.setattr(settings, "work_windows", "09:00-12:00,14:00-18:00")
    monkeypatch.setattr(settings, "min_send_interval_seconds", 90)

    day = date(2026, 9, 15)
    now = datetime(2026, 9, 15, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    tasks = [
        _task("a", "P0"),
        _task("b", "P0"),
        _task("c", "P1"),
        _task("d", "P1"),
        _task("e", "P2"),
    ]
    items = schedule_tasks(tasks, day, rng=Random(42), now=now)
    assert len(items) == 5
    times = [datetime.fromisoformat(i.scheduled_at) for i in items]
    assert times == sorted(times)
    for a, b in zip(times, times[1:]):
        assert (b - a).total_seconds() >= 90

    # All P0 finish before any P2? Not guaranteed by slot sampling across full day,
    # but P0 are scheduled before P1/P2 in assignment order into chosen slots.
    # We assert priorities present and windows valid.
    assert {i.priority for i in items} == {"P0", "P1", "P2"}


def test_ny_today_boundary():
    # 2026-09-15 03:10 UTC = 2026-09-14 23:10 America/New_York (EDT)
    utc = datetime(2026, 9, 15, 3, 10, tzinfo=timezone.utc)
    assert ny_today(utc, "America/New_York") == date(2026, 9, 14)
    utc2 = datetime(2026, 9, 15, 4, 10, tzinfo=timezone.utc)  # 00:10 EDT
    assert ny_today(utc2, "America/New_York") == date(2026, 9, 15)


def test_plan_persist_and_due(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    plan = DailyPlan(
        day="2026-09-15",
        timezone="America/New_York",
        created_at="x",
        items=[
            PlanItem(
                task_id="t1",
                title="one",
                priority="P0",
                scheduled_at="2026-09-15T09:00:00-04:00",
                status="planned",
            ),
            PlanItem(
                task_id="t2",
                title="two",
                priority="P1",
                scheduled_at="2026-09-15T15:00:00-04:00",
                status="planned",
            ),
            PlanItem(
                task_id="t3",
                title="done",
                priority="P1",
                scheduled_at="2026-09-15T09:05:00-04:00",
                status="completed",
            ),
        ],
    )
    path = save_plan(plan)
    assert path.exists()
    loaded = load_plan("2026-09-15")
    assert loaded and len(loaded.items) == 3
    now = datetime(2026, 9, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    due = due_items(loaded, now=now)
    assert [d.task_id for d in due] == ["t1"]


def test_recheck_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    plan = DailyPlan(
        day="2026-09-15",
        timezone="America/New_York",
        created_at="x",
        items=[
            PlanItem(
                task_id="t-skip",
                title="skipme",
                priority="P0",
                scheduled_at="2026-09-15T09:00:00-04:00",
                status="planned",
            )
        ],
    )
    save_plan(plan)
    notion = MagicMock()
    notion.get_task_status.return_value = "Cancelled"
    item = plan.items[0]
    result = execute_plan_item(item, plan, notion=notion, dry_run=True)
    assert result["status"] == "skipped"
    assert item.status == "skipped"
    notion.resolve_task_for_send.assert_not_called()


def test_outbound_success_updates_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "saily_phone_e164", "+18207863604")
    notion = MagicMock()
    gateway = MagicMock()
    gateway.send_sms.return_value = {"id": "gw-1"}
    cache = OutboundCache(channel="SMS", path=tmp_path / "last_outbound_by_phone_sms.json")
    resolved = ResolvedTask(
        task_id="task-1",
        title="t",
        priority="P0",
        scheduled_at="2026-09-15",
        status="Pending",
        contact_id="contact-1",
        keyperson_id="kp-1",
        phone_e164="+17372583742",
        conversation_id="conv-1",
        content="hello from plan",
        thread_id="THR-test-SMS",
    )
    result = execute_resolved(resolved, notion=notion, gateway=gateway, cache=cache)
    assert result["ok"] is True
    notion.update_task_status.assert_any_call("task-1", "In Progress")
    # Completed with ended_at
    completed_calls = [
        c for c in notion.update_task_status.call_args_list if c.args[1] == "Completed"
    ]
    assert completed_calls
    entry = cache.get("+17372583742")
    assert entry and entry["task_page_id"] == "task-1"
    assert entry["state"] == "ready"
    assert entry["thread_id"] == "THR-test-SMS"


def test_outbound_missing_content_fails():
    notion = MagicMock()
    resolved = ResolvedTask(
        task_id="task-2",
        title="t",
        priority="P0",
        scheduled_at="2026-09-15",
        status="Pending",
        contact_id="c",
        keyperson_id="k",
        phone_e164="+17372583742",
        conversation_id=None,
        content=None,
        resolve_error="缺少关联 Conversation，无发送正文",
    )
    result = execute_resolved(resolved, notion=notion, gateway=MagicMock())
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert notion.update_task_status.call_args.args[1] == "Failed"


def test_inbound_match_and_webhook(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")  # skip notion create
    cache = OutboundCache(channel="SMS", path=tmp_path / "last_outbound_by_phone_sms.json")
    cache.set_ready(
        "+17372583742",
        task_page_id="task-abc",
        thread_id="THR-abc-SMS",
        contact_page_id="contact-abc",
        conversation_page_id="conv-out",
    )
    wh = MagicMock()
    wh.build_sms_payload.return_value = {
        "taskId": "task-abc",
        "threadId": "THR-abc-SMS",
        "content": "reply yes",
        "channel": "SMS",
        "messageId": "",
    }
    wh.post_reply.return_value = {"ok": True}
    result = handle_inbound_sms(
        {"sender": "+17372583742", "body": "reply yes", "message_id": "m1"},
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    assert result["task_page_id"] == "task-abc"
    assert result["thread_id"] == "THR-abc-SMS"
    wh.build_sms_payload.assert_called_once()
    assert wh.build_sms_payload.call_args.kwargs["task_id"] == "task-abc"
    assert wh.build_sms_payload.call_args.kwargs["thread_id"] == "THR-abc-SMS"
    wh.post_reply.assert_called_once()
    assert wh.post_reply.call_args.args[0]["messageId"] == ""


def test_inbound_unmatched(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    cache = OutboundCache(channel="SMS", path=tmp_path / "last_outbound_by_phone_sms.json")
    wh = MagicMock()
    result = handle_inbound_sms(
        {"sender": "+19999999999", "body": "hi"},
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is False
    assert result["task_page_id"] is None
    wh.post_reply.assert_not_called()


def test_inbound_pending_still_matches_reply(tmp_path, monkeypatch):
    """Pending is matchable (WA replies during send / legacy rows)."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    cache = OutboundCache(channel="SMS", path=tmp_path / "c.json")
    cache.set_pending(
        "+15551212121",
        task_page_id="t1",
        thread_id="THR-pending",
        contact_page_id="c",
        conversation_page_id="v1",
    )
    wh = MagicMock()
    wh.build_sms_payload.return_value = {"channel": "SMS"}
    wh.post_reply.return_value = {"ok": True}
    result = handle_inbound_sms(
        {"sender": "+15551212121", "body": "during send"},
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    wh.post_reply.assert_called_once()


def test_same_phone_cache_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    cache = OutboundCache(channel="SMS", path=tmp_path / "c.json")
    cache.set_ready(
        "+15551212121",
        task_page_id="t1",
        thread_id="THR-1",
        contact_page_id="c",
        conversation_page_id="v1",
    )
    cache.set_ready(
        "+15551212121",
        task_page_id="t2",
        thread_id="THR-2",
        contact_page_id="c",
        conversation_page_id="v2",
    )
    assert cache.get("5551212121")["task_page_id"] == "t2"
    assert cache.get_ready_for_reply("+15551212121")["thread_id"] == "THR-2"


def test_wa_job_queue_lease_and_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    from channel_orchestrator.wa_jobs import WaJobQueue

    q = WaJobQueue(path=tmp_path / "wa_jobs.json")
    j = q.enqueue({"phone": "+15551212", "text": "hi", "task_id": "t1"})
    nxt = q.next_job()
    assert nxt and nxt["id"] == j["id"] and nxt["status"] == "leased"
    assert q.next_job() is None
    done = q.complete(j["id"], ok=True)
    assert done and done["status"] == "done"


def test_wa_inbound_uses_whatsapp_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    from channel_orchestrator.inbound import handle_inbound_whatsapp

    cache = OutboundCache(channel="WHATSAPP", path=tmp_path / "wa_cache.json")
    cache.set_ready(
        "+17372583742",
        task_page_id="wa-task",
        thread_id="THR-wa-1",
        contact_page_id="c",
        conversation_page_id="v",
    )
    wh = MagicMock()
    wh.build_whatsapp_payload.return_value = {
        "taskId": "wa-task",
        "threadId": "THR-wa-1",
        "content": "ok",
        "channel": "WhatsApp",
        "messageId": "",
    }
    wh.post_reply.return_value = {"ok": True}
    result = handle_inbound_whatsapp(
        {"sender": "+17372583742", "body": "ok"},
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    wh.build_whatsapp_payload.assert_called_once()
    assert wh.build_whatsapp_payload.call_args.kwargs["thread_id"] == "THR-wa-1"
    wh.post_reply.assert_called_once()
    assert wh.post_reply.call_args.args[0]["channel"] == "WhatsApp"
    assert wh.post_reply.call_args.args[0]["messageId"] == ""


def test_wa_inbound_matches_legacy_pending_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    from channel_orchestrator.inbound import handle_inbound_whatsapp

    cache = OutboundCache(channel="WHATSAPP", path=tmp_path / "wa_cache.json")
    cache.set_pending(
        "+8615810494081",
        task_page_id="wa-pending-task",
        thread_id="THR-pending",
        contact_page_id="c",
        conversation_page_id="v",
    )
    wh = MagicMock()
    wh.build_whatsapp_payload.return_value = {
        "taskId": "wa-pending-task",
        "threadId": "THR-pending",
        "content": "early reply",
        "channel": "WhatsApp",
        "messageId": "",
    }
    wh.post_reply.return_value = {"ok": True}
    result = handle_inbound_whatsapp(
        {"sender": "+8615810494081", "body": "early reply"},
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    wh.post_reply.assert_called_once()


def test_reply_webhook_forces_empty_message_id(monkeypatch):
    from channel_orchestrator.reply_webhook import ReplyWebhookClient

    monkeypatch.setattr(settings, "reply_webhook_url", "")
    client = ReplyWebhookClient()
    payload = client.build_sms_payload(
        task_id="t1",
        thread_id="THR-1",
        content="hi",
        sender="+1555",
        occurred_at="2026-09-14T12:00:00Z",
        sms_message_sid="SID-9",
    )
    assert payload["messageId"] == ""
    assert payload["extendedParameters"]["smsMessageSid"] == "SID-9"
    result = client.post_reply({**payload, "messageId": "must-be-cleared"})
    assert result["skipped"] is True
    assert result["reason"] == "no_url"


def test_reply_webhook_whatsapp_media_extended():
    from channel_orchestrator.reply_webhook import ReplyWebhookClient

    client = ReplyWebhookClient()
    payload = client.build_whatsapp_payload(
        task_id="t-wa",
        thread_id="THR-wa",
        content="",
        sender="+15551212",
        occurred_at="2026-09-17T12:00:00Z",
        media_url="https://cdn.example.com/wa-inbound/image/a.jpg",
        media_type="image",
        media_content_type="image/jpeg",
        media_filename="a.jpg",
    )
    assert payload["content"] == "https://cdn.example.com/wa-inbound/image/a.jpg"
    assert payload["extendedParameters"]["mediaUrl"].endswith("a.jpg")
    assert payload["extendedParameters"]["mediaType"] == "image"
    assert payload["extendedParameters"]["mediaFilename"] == "a.jpg"

    with_caption = client.build_whatsapp_payload(
        task_id="t-wa",
        thread_id="THR-wa",
        content="look at this",
        sender="+15551212",
        occurred_at="2026-09-17T12:00:00Z",
        media_url="https://cdn.example.com/wa-inbound/image/a.jpg",
        media_type="image",
    )
    assert with_caption["content"] == "look at this\nhttps://cdn.example.com/wa-inbound/image/a.jpg"


def test_filter_query_shape_documented():
    """T1 helper: filter object used by NotionClient.query_today_sms_pending."""
    day = "2026-09-14"
    filt = {
        "and": [
            {"property": "Channel", "select": {"equals": "SMS"}},
            {"property": "Task Status", "status": {"equals": "Pending"}},
            {"property": "Scheduled At", "date": {"equals": day}},
        ]
    }
    assert filt["and"][0]["select"]["equals"] == "SMS"


def test_normalize_channel_email():
    from channel_orchestrator.channels import channel_display_name, normalize_channel

    assert normalize_channel("Email") == "EMAIL"
    assert normalize_channel("gmail") == "EMAIL"
    assert channel_display_name("EMAIL") == "Email"


def test_normalize_email_angle_addr():
    from channel_orchestrator.email_util import normalize_email

    assert normalize_email("Alice <Alice@Example.com>") == "alice@example.com"
    assert normalize_email("bad") is None


def test_email_outbound_new_thread(tmp_path, monkeypatch):
    from channel_orchestrator.outbound import execute_resolved_email

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "gmail_user", "mark@fridgechannels.com")
    notion = MagicMock()
    gmail = MagicMock()
    gmail.send_new_or_reply.return_value = {
        "gmail_thread_id": "gt-1",
        "gmail_message_id": "gm-1",
    }
    cache = OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")
    resolved = ResolvedTask(
        task_id="task-e1",
        title="Hello",
        priority="P0",
        scheduled_at="2026-09-15",
        status="Pending",
        contact_id="c1",
        keyperson_id="k1",
        phone_e164=None,
        conversation_id="conv-e1",
        content="Body text",
        thread_id="THR-e1-Email",
        channel="EMAIL",
        email="prospect@example.com",
        subject="Hello",
        extended_parameters={},
    )
    result = execute_resolved_email(resolved, notion=notion, gmail=gmail, cache=cache)
    assert result["ok"] is True
    gmail.send_new_or_reply.assert_called_once()
    assert gmail.send_new_or_reply.call_args.kwargs["gmail_thread_id"] is None
    notion.update_conversation_extended_parameters.assert_called()
    ext_arg = notion.update_conversation_extended_parameters.call_args.args[1]
    assert ext_arg["gmailThreadId"] == "gt-1"
    entry = cache.get_ready_for_reply("prospect@example.com")
    assert entry and entry["gmail_thread_id"] == "gt-1"


def test_email_outbound_reply_in_thread(tmp_path, monkeypatch):
    from channel_orchestrator.outbound import execute_resolved_email

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    notion = MagicMock()
    gmail = MagicMock()
    gmail.send_new_or_reply.return_value = {
        "gmail_thread_id": "gt-existing",
        "gmail_message_id": "gm-2",
    }
    cache = OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")
    resolved = ResolvedTask(
        task_id="task-e2",
        title="Follow",
        priority="P0",
        scheduled_at="2026-09-15",
        status="Pending",
        contact_id="c1",
        keyperson_id="k1",
        phone_e164=None,
        conversation_id="conv-e2",
        content="Follow up",
        thread_id="THR-e2-Email",
        channel="EMAIL",
        email="prospect@example.com",
        subject="Follow",
        extended_parameters={"gmailThreadId": "gt-existing"},
    )
    result = execute_resolved_email(resolved, notion=notion, gmail=gmail, cache=cache)
    assert result["ok"] is True
    assert gmail.send_new_or_reply.call_args.kwargs["gmail_thread_id"] == "gt-existing"


def test_email_inbound_matches_gmail_thread(tmp_path, monkeypatch):
    from channel_orchestrator.inbound import handle_inbound_email

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    cache = OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")
    cache.set_ready(
        "prospect@example.com",
        task_page_id="task-e",
        thread_id="THR-e-Email",
        contact_page_id="c",
        conversation_page_id="v",
        extra={"gmail_thread_id": "gt-9", "gmailThreadId": "gt-9"},
    )
    wh = MagicMock()
    wh.build_email_payload.return_value = {
        "taskId": "task-e",
        "threadId": "THR-e-Email",
        "content": "Thanks",
        "channel": "Email",
        "messageId": "",
        "extendedParameters": {"gmailThreadId": "gt-9", "gmailMessageId": "gm-in"},
    }
    wh.post_reply.return_value = {"ok": True}
    result = handle_inbound_email(
        {
            "sender": "Other <prospect@example.com>",
            "body": "Thanks",
            "subject": "Re: Hello",
            "gmail_thread_id": "gt-9",
            "gmail_message_id": "gm-in",
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    wh.build_email_payload.assert_called_once()
    assert wh.build_email_payload.call_args.kwargs["gmail_thread_id"] == "gt-9"
    assert wh.post_reply.call_args.args[0]["messageId"] == ""


def test_email_hard_bounce_fails_task(tmp_path, monkeypatch):
    from channel_orchestrator.inbound import handle_inbound_email

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    cache = OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")
    cache.set_ready(
        "prospect@example.com",
        task_page_id="task-bounce",
        thread_id="THR-b",
        contact_page_id="c",
        conversation_page_id="conv-b",
        extra={"gmail_thread_id": "gt-bounce"},
    )
    notion = MagicMock()
    wh = MagicMock()
    result = handle_inbound_email(
        {
            "sender": "mailer-daemon@google.com",
            "body": "550 5.1.1 The email account that you tried to reach does not exist",
            "subject": "Delivery Status Notification (Failure)",
            "gmail_thread_id": "gt-bounce",
            "classify": {
                "kind": "hard_bounce",
                "reason": "Email硬退信：地址无效或永久拒收",
                "is_human": False,
            },
        },
        notion=notion,
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result.get("bounce") is True
    assert result.get("kind") == "hard_bounce"
    notion.update_task_status.assert_called()
    assert notion.update_task_status.call_args.args[1] == "Failed"
    notes = notion.update_task_status.call_args.kwargs.get("notes") or ""
    assert "硬退信" in notes
    wh.post_reply.assert_not_called()
    assert cache.get("prospect@example.com")["state"] == "failed"


def test_email_classify_hard_vs_soft():
    from channel_orchestrator.email_classify import classify_inbound_email

    hard = classify_inbound_email(
        sender="mailer-daemon@google.com",
        subject="Undeliverable: Hello",
        body="550 5.1.1 user unknown",
    )
    assert hard["kind"] == "hard_bounce"
    soft = classify_inbound_email(
        sender="mailer-daemon@google.com",
        subject="Delayed mail",
        body="mailbox full try again later",
    )
    assert soft["kind"] == "soft_bounce"
    human = classify_inbound_email(
        sender="boss@acme.com",
        subject="Re: Hello",
        body="Sounds good",
    )
    assert human["kind"] == "human"


def test_email_proxy_reply_same_org_thread(tmp_path, monkeypatch):
    from channel_orchestrator.inbound import handle_inbound_email

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    monkeypatch.setattr(settings, "email_allow_cross_domain_thread_reply", True)
    cache = OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")
    cache.set_ready(
        "a@acme.com",
        task_page_id="task-a",
        thread_id="THR-a",
        contact_page_id="c",
        conversation_page_id="v",
        extra={"gmail_thread_id": "gt-proxy"},
    )
    wh = MagicMock()
    wh.build_email_payload.side_effect = lambda **kw: {
        "taskId": kw["task_id"],
        "threadId": kw["thread_id"],
        "content": kw["content"],
        "channel": "Email",
        "messageId": "",
        "extendedParameters": kw.get("extra_extended") or {},
    }
    wh.post_reply.return_value = {"ok": True}
    result = handle_inbound_email(
        {
            "sender": "b@acme.com",
            "body": "I will take this",
            "subject": "Re: Hello",
            "gmail_thread_id": "gt-proxy",
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    assert result["reply_meta"]["proxyReply"] is True
    assert result["reply_meta"]["sameOrgDomain"] is True
    ext = wh.post_reply.call_args.args[0]["extendedParameters"]
    assert ext["proxyReply"] is True
    assert ext["outboundTo"] == "a@acme.com"
    assert ext["replyFrom"] == "b@acme.com"


def test_email_unexpected_sender_same_thread(tmp_path, monkeypatch):
    from channel_orchestrator.inbound import handle_inbound_email

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    monkeypatch.setattr(settings, "email_allow_cross_domain_thread_reply", True)
    cache = OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")
    cache.set_ready(
        "a@acme.com",
        task_page_id="task-a",
        thread_id="THR-a",
        contact_page_id="c",
        conversation_page_id="v",
        extra={"gmail_thread_id": "gt-x"},
    )
    wh = MagicMock()
    wh.build_email_payload.side_effect = lambda **kw: {
        "taskId": kw["task_id"],
        "threadId": kw["thread_id"],
        "content": kw["content"],
        "channel": "Email",
        "messageId": "",
        "extendedParameters": kw.get("extra_extended") or {},
    }
    wh.post_reply.return_value = {"ok": True}
    result = handle_inbound_email(
        {
            "sender": "stranger@gmail.com",
            "body": "hi",
            "gmail_thread_id": "gt-x",
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    assert result["reply_meta"]["unexpectedSender"] is True
    assert result["reply_meta"]["sameOrgDomain"] is False


def test_email_same_domain_without_thread_does_not_match(tmp_path, monkeypatch):
    from channel_orchestrator.inbound import handle_inbound_email

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    cache = OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")
    cache.set_ready(
        "a@acme.com",
        task_page_id="task-a",
        thread_id="THR-a",
        contact_page_id="c",
        conversation_page_id="v",
        extra={"gmail_thread_id": "gt-only"},
    )
    wh = MagicMock()
    # Avoid real /api/inbound; cold path is covered in test_email_cold_inbound.py
    portal = MagicMock()
    portal.post_inbound.return_value = {"ok": True, "skipped": True}
    monkeypatch.setattr(
        "channel_orchestrator.email_cold_inbound.InboundPortalClient",
        lambda: portal,
    )
    result = handle_inbound_email(
        {
            "sender": "b@acme.com",
            "body": "no thread link",
            # no gmail_thread_id → must not match by domain alone
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is False
    wh.post_reply.assert_not_called()
    assert result.get("cold") is True
    portal.post_inbound.assert_called()