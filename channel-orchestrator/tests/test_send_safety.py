from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from channel_orchestrator.config import settings
from channel_orchestrator.outbound import execute_plan_item, execute_resolved_sms, finalize_whatsapp_job
from channel_orchestrator.scheduler import DailyPlan, PlanItem
from channel_orchestrator.send_safety import (
    UNCERTAIN_NOTES_PREFIX,
    classify_sms_send_error,
    is_uncertain_error,
    record_attempt,
    retry_blocked_reason,
    uncertain_notes,
)
from channel_orchestrator.notion_client import ResolvedTask


def test_classify_sms_errors():
    assert classify_sms_send_error("502 Bad Gateway") == "hard"
    assert classify_sms_send_error(TimeoutError("timed out")) == "uncertain"
    assert classify_sms_send_error("connection reset by peer") == "uncertain"


def test_uncertain_error_detection():
    assert is_uncertain_error("UNCERTAIN:发送超时")
    assert is_uncertain_error("发送未确认（无障碍未点到发送或超时）")
    assert not is_uncertain_error("号码未注册 WhatsApp")


def test_sms_timeout_stays_in_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    notion = MagicMock()
    gateway = MagicMock()
    gateway.send_sms.side_effect = TimeoutError("read timed out")
    resolved = ResolvedTask(
        task_id="task-u1",
        title="t",
        priority="P0",
        scheduled_at="2026-09-15",
        status="Pending",
        contact_id="c",
        keyperson_id="k",
        phone_e164="+17372583742",
        conversation_id="conv-1",
        content="hi",
        thread_id="THR-1-SMS",
    )
    result = execute_resolved_sms(resolved, notion=notion, gateway=gateway)
    assert result["status"] == "uncertain"
    # Last status update should be In Progress with uncertain notes (not Failed)
    last = notion.update_task_status.call_args_list[-1]
    assert last.args[1] == "In Progress"
    assert UNCERTAIN_NOTES_PREFIX in (last.kwargs.get("notes") or "")


def test_sms_hard_fail_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    notion = MagicMock()
    gateway = MagicMock()
    gateway.send_sms.side_effect = RuntimeError("SMS Gateway POST /message -> 502: Bad Gateway")
    resolved = ResolvedTask(
        task_id="task-h1",
        title="t",
        priority="P0",
        scheduled_at="2026-09-15",
        status="Pending",
        contact_id="c",
        keyperson_id="k",
        phone_e164="+17372583742",
        conversation_id="conv-1",
        content="hi",
        thread_id="THR-1-SMS",
    )
    result = execute_resolved_sms(resolved, notion=notion, gateway=gateway)
    assert result["status"] == "failed"
    assert notion.update_task_status.call_args.args[1] == "Failed"


def test_sms_requires_gateway_message_id(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    notion = MagicMock()
    gateway = MagicMock()
    gateway.send_sms.return_value = {"status_code": 202}
    resolved = ResolvedTask(
        task_id="task-noid",
        title="t",
        priority="P0",
        scheduled_at="2026-09-15",
        status="Pending",
        contact_id="c",
        keyperson_id="k",
        phone_e164="+17372583742",
        conversation_id="conv-1",
        content="hi",
        thread_id="THR-1-SMS",
    )
    result = execute_resolved_sms(resolved, notion=notion, gateway=gateway)
    assert result["status"] == "uncertain"


def test_wa_uncertain_finalize(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    notion = MagicMock()
    result = finalize_whatsapp_job(
        {
            "id": "job-1",
            "task_id": "task-wa",
            "conversation_id": "conv-wa",
            "phone": "+17372583742",
            "thread_id": "THR-wa",
        },
        ok=False,
        error="UNCERTAIN:发送超时（35s 未确认，请勿直接重试）",
        notion=notion,
    )
    assert result["status"] == "uncertain"
    assert notion.update_task_status.call_args.args[1] == "In Progress"
    notion.update_conversation_outbound.assert_not_called()


def test_wa_enqueue_marks_cache_ready_for_early_replies(tmp_path, monkeypatch):
    """WA replies may arrive before Companion ACK; cache must be ready at enqueue."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "wa_daily_send_limit", 0)
    from channel_orchestrator.cache import OutboundCache
    from channel_orchestrator.notion_client import ResolvedTask
    from channel_orchestrator.outbound import enqueue_whatsapp_job
    from channel_orchestrator.wa_jobs import WaJobQueue

    notion = MagicMock()
    cache = OutboundCache(channel="WHATSAPP", path=tmp_path / "wa_cache.json")
    monkeypatch.setattr(
        "channel_orchestrator.outbound.get_wa_queue",
        lambda: WaJobQueue(path=tmp_path / "wa_jobs.json"),
    )
    resolved = ResolvedTask(
        task_id="task-wa-early",
        title="t",
        priority="P1",
        scheduled_at="2026-09-17",
        status="Pending",
        contact_id="c",
        keyperson_id="k",
        phone_e164="+8615810494081",
        conversation_id="conv-wa",
        content="hello",
        thread_id="THR-wa-early",
        channel="WHATSAPP",
    )
    result = enqueue_whatsapp_job(resolved, notion=notion, cache=cache)
    assert result["status"] == "queued"
    ready = cache.get_ready_for_reply("+8615810494081")
    assert ready is not None
    assert ready["state"] == "ready"
    assert ready["task_page_id"] == "task-wa-early"
    assert ready["thread_id"] == "THR-wa-early"


def test_retry_blocked_by_uncertain_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    notion = MagicMock()
    notion.get_page.return_value = {
        "properties": {
            "Notes": {
                "type": "rich_text",
                "rich_text": [{"plain_text": uncertain_notes("先前超时"), "type": "text", "text": {"content": uncertain_notes("先前超时")}}],
            },
            "Interaction At": {"type": "date", "date": None},
        }
    }
    reason = retry_blocked_reason(
        notion=notion, task_id="task-1", conversation_id="conv-1", force=False
    )
    assert reason and UNCERTAIN_NOTES_PREFIX in reason
    assert (
        retry_blocked_reason(
            notion=notion, task_id="task-1", conversation_id="conv-1", force=True
        )
        is None
    )


def test_retry_blocked_by_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "outbound_uncertain_cooldown_seconds", 3600)
    record_attempt("task-cool", channel="SMS", outcome="uncertain", detail="timeout")
    notion = MagicMock()
    notion.get_page.return_value = {
        "properties": {
            "Notes": {"type": "rich_text", "rich_text": []},
            "Interaction At": {"type": "date", "date": None},
        }
    }
    reason = retry_blocked_reason(
        notion=notion, task_id="task-cool", conversation_id="conv-1", force=False
    )
    assert reason and "冷却期" in reason


def test_execute_plan_item_skips_sent_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    notion = MagicMock()
    notion.get_task_status.return_value = "Pending"
    notion.resolve_task_for_send.return_value = ResolvedTask(
        task_id="task-sent",
        title="t",
        priority="P0",
        scheduled_at="2026-09-15",
        status="Pending",
        contact_id="c",
        keyperson_id="k",
        phone_e164="+17372583742",
        conversation_id="conv-sent",
        content="hi",
        thread_id="THR-s",
    )

    def get_page(page_id: str):
        if page_id == "conv-sent":
            return {
                "properties": {
                    "Interaction At": {
                        "type": "date",
                        "date": {"start": "2026-09-15T12:00:00.000Z"},
                    },
                    "Notes": {
                        "type": "rich_text",
                        "rich_text": [
                            {"plain_text": "已发送。", "type": "text", "text": {"content": "已发送。"}}
                        ],
                    },
                }
            }
        return {
            "properties": {
                "Notes": {"type": "rich_text", "rich_text": []},
                "Interaction At": {"type": "date", "date": None},
            }
        }

    notion.get_page.side_effect = get_page
    plan = DailyPlan(
        day="2026-09-15",
        channel="SMS",
        timezone="America/New_York",
        created_at=datetime.now(timezone.utc).isoformat(),
        items=[],
    )
    item = PlanItem(
        task_id="task-sent",
        title="t",
        priority="P0",
        scheduled_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        status="planned",
        channel="SMS",
        conversation_id="conv-sent",
    )
    plan.items.append(item)
    result = execute_plan_item(item, plan, notion=notion, gateway=MagicMock())
    assert result["status"] == "skipped"
    assert "已发送" in (result.get("reason") or "")
