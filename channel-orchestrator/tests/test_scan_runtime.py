from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from channel_orchestrator.notion_props import is_scheduled_due, parse_notion_datetime
from channel_orchestrator.outbound_queue import OutboundQueue
from channel_orchestrator.runtime_settings import load_runtime_settings, save_runtime_settings


def test_parse_notion_datetime_with_offset():
    prop = {
        "date": {
            "start": "2026-09-17T09:30:00.000-04:00",
            "time_zone": "America/New_York",
        }
    }
    dt = parse_notion_datetime(prop)
    assert dt is not None
    assert dt.utcoffset().total_seconds() == -4 * 3600
    assert dt.hour == 9
    assert dt.minute == 30


def test_parse_notion_datetime_date_only_uses_timezone():
    prop = {"date": {"start": "2026-09-17", "time_zone": "America/New_York"}}
    dt = parse_notion_datetime(prop, default_tz="UTC")
    assert dt is not None
    assert dt.tzinfo == ZoneInfo("America/New_York")
    assert dt.hour == 0


def test_is_scheduled_due_respects_timezone():
    # 10:00 America/New_York = 14:00 UTC
    prop = {
        "date": {
            "start": "2026-09-17T10:00:00.000-04:00",
            "time_zone": "America/New_York",
        }
    }
    before = datetime(2026, 9, 17, 13, 59, tzinfo=timezone.utc)
    after = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    assert is_scheduled_due(prop, now=before) is False
    assert is_scheduled_due(prop, now=after) is True


def test_outbound_queue_email_and_phone(tmp_path, monkeypatch):
    from channel_orchestrator import config

    monkeypatch.setattr(config.settings, "data_dir", str(tmp_path))
    q = OutboundQueue(path=tmp_path / "outbound_queue.json")
    assert q.enqueue(task_id="e1", channel="EMAIL", title="e")
    assert q.enqueue(task_id="s1", channel="SMS", title="s")
    assert q.enqueue(task_id="e1", channel="EMAIL") is None  # duplicate active

    emails = q.pop_emails()
    assert len(emails) == 1
    assert emails[0]["task_id"] == "e1"
    phone = q.pop_next_phone()
    assert phone and phone["task_id"] == "s1"
    q.finish(emails[0]["id"], status="done")
    q.finish(phone["id"], status="done")
    assert q.depth()["total"] == 0


def test_runtime_settings_persist(tmp_path, monkeypatch):
    from channel_orchestrator import config

    monkeypatch.setattr(config.settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(config.settings, "scheduler_scan_seconds", 600)
    monkeypatch.setattr(config.settings, "send_gap_seconds", 90)
    save_runtime_settings(scan_interval_seconds=120, send_gap_seconds=45)
    loaded = load_runtime_settings()
    assert loaded["scan_interval_seconds"] == 120
    assert loaded["send_gap_seconds"] == 45
