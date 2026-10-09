from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from channel_orchestrator.cache import OutboundCache
from channel_orchestrator.config import settings
from channel_orchestrator.external_send import ExternalSendError, register_external_email_sent
from channel_orchestrator.inbound import handle_inbound_email
from channel_orchestrator.server import app

SUBJECT = "Rotten (candy) FridgeChannel sample is live, great meeting you at NACS Show 2026"


def _cache(tmp_path, monkeypatch) -> OutboundCache:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    return OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")


def _webhook() -> MagicMock:
    wh = MagicMock()
    wh.build_email_payload.side_effect = lambda **kw: {"taskId": kw["task_id"], "threadId": kw["thread_id"]}
    wh.post_reply.return_value = {"ok": True}
    return wh


def _register(cache: OutboundCache, **overrides):
    kw = {
        "email": "Brandon@EatRotten.com",
        "task_id": "task-rotten",
        "thread_id": "THR-rotten-Email",
        "gmail_thread_id": "gt-sent",
        "gmail_message_id": "gm-sent",
        "subject": SUBJECT,
        "sent_at": "2026-10-07T21:51:18Z",
        "conversation_page_id": "conv-rotten",
        "cache": cache,
    }
    kw.update(overrides)
    return register_external_email_sent(**kw)


def test_register_writes_ready_row_matching_orchestrator_shape(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    result = _register(cache)
    assert result["registered"] is True
    row = cache.get("brandon@eatrotten.com")
    assert row["state"] == "ready"
    assert row["task_page_id"] == "task-rotten"
    assert row["thread_id"] == "THR-rotten-Email"
    assert row["gmail_thread_id"] == row["gmailThreadId"] == "gt-sent"
    assert row["subject"] == SUBJECT
    assert row["sent_at"].startswith("2026-10-07T21:51:18")


def test_auto_reply_in_new_thread_matches_registered_send(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _register(cache)
    wh = _webhook()
    result = handle_inbound_email(
        {
            "sender": "brandon@eatrotten.com",
            "subject": f"I'm at NACS! Re: {SUBJECT}",
            "body": "Back in the office Oct 12.",
            "gmail_thread_id": "gt-auto",
            "classify": {"kind": "auto_reply", "reason": "auto", "is_human": False},
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    assert wh.build_email_payload.call_args.kwargs["task_id"] == "task-rotten"


def test_bounce_in_sent_thread_fails_registered_task(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _register(cache, email="dominique@richlandifc.com", task_id="task-dom", thread_id="THR-dom")
    notion = MagicMock()
    result = handle_inbound_email(
        {
            "sender": "mailer-daemon@googlemail.com",
            "subject": "Delivery Status Notification (Failure)",
            "body": "Message blocked",
            "gmail_thread_id": "gt-sent",
            "failed_recipients": ["dominique@richlandifc.com"],
            "classify": {"kind": "hard_bounce", "reason": "Email硬退信", "is_human": False},
        },
        notion=notion,
        cache=cache,
        reply_webhook=_webhook(),
        write_notion=False,
    )
    assert result["matched"] is True
    assert notion.update_task_status.call_args.args[:2] == ("task-dom", "Failed")


def test_does_not_replace_newer_send_for_other_task(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _register(cache, task_id="task-new", sent_at="2026-10-09T10:00:00Z")
    result = _register(cache, task_id="task-old", sent_at="2026-10-07T21:51:18Z")
    assert result["registered"] is False
    assert result["reason"] == "newer_outbound_exists"
    assert cache.get("brandon@eatrotten.com")["task_page_id"] == "task-new"


def test_later_send_replaces_older_row(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _register(cache, task_id="task-old", sent_at="2026-10-07T21:51:18Z")
    assert _register(cache, task_id="task-new", sent_at="2026-10-09T10:00:00Z")["registered"] is True
    assert cache.get("brandon@eatrotten.com")["task_page_id"] == "task-new"


def test_same_task_registration_is_idempotent(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    _register(cache, sent_at="2026-10-09T10:00:00Z")
    assert _register(cache, sent_at="2026-10-07T21:51:18Z")["registered"] is True
    assert cache.get("brandon@eatrotten.com")["task_page_id"] == "task-rotten"


def test_missing_gmail_details_are_filled_from_gmail(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    gmail = MagicMock()
    gmail.get_message.return_value = {
        "threadId": "gt-from-api",
        "internalDate": "1791409878000",
        "payload": {"headers": [{"name": "Subject", "value": SUBJECT}]},
    }
    result = _register(cache, gmail_thread_id=None, subject=None, sent_at=None, gmail=gmail)
    assert result["gmail_thread_id"] == "gt-from-api"
    assert result["subject"] == SUBJECT
    gmail.get_message.assert_called_once_with("gm-sent", format="metadata")


@pytest.mark.parametrize(
    "overrides",
    [{"email": "not-an-email"}, {"task_id": ""}, {"thread_id": ""}, {"sent_at": "yesterday"}],
)
def test_invalid_input_rejected(tmp_path, monkeypatch, overrides):
    cache = _cache(tmp_path, monkeypatch)
    with pytest.raises(ExternalSendError):
        _register(cache, **overrides)


def test_endpoint_requires_token_and_registers(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "external_send_api_token", "secret")
    client = TestClient(app)
    body = {
        "email": "brandon@eatrotten.com",
        "taskId": "task-rotten",
        "threadId": "THR-rotten-Email",
        "gmailThreadId": "gt-sent",
        "gmailMessageId": "gm-sent",
        "subject": SUBJECT,
        "sentAt": "2026-10-07T21:51:18Z",
        "source": "superExhibition",
    }
    assert client.post("/api/email/external-sent", json=body).status_code == 401
    r = client.post("/api/email/external-sent", json=body, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    assert r.json()["registered"] is True
    row = OutboundCache(channel="EMAIL").get("brandon@eatrotten.com")
    assert row["task_page_id"] == "task-rotten"
    assert row["source"] == "superExhibition"

    bad = client.post(
        "/api/email/external-sent",
        json={**body, "threadId": ""},
        headers={"Authorization": "Bearer secret"},
    )
    assert bad.status_code == 400


def test_endpoint_unavailable_without_configured_token(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "external_send_api_token", "")
    monkeypatch.setattr(settings, "wa_api_token", "")
    r = TestClient(app).post("/api/email/external-sent", json={})
    assert r.status_code == 503
