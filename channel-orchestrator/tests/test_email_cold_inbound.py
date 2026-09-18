"""Email cold inbound (no Task) → POST /api/inbound."""

from __future__ import annotations

from unittest.mock import MagicMock

from channel_orchestrator.client_domain_cache import (
    FollowUpClientDomainCache,
    extract_domains_from_client,
)
from channel_orchestrator.config import settings
from channel_orchestrator.email_util import (
    collect_external_emails,
    extract_emails_from_header_value,
    normalize_domain,
)
from channel_orchestrator.inbound_portal import InboundPortalClient


def test_normalize_domain():
    assert normalize_domain("https://www.Acme.com/path") == "acme.com"
    assert normalize_domain("buyer@acme.com") == "acme.com"
    assert normalize_domain("gmail.com") == "gmail.com"
    assert normalize_domain("") is None


def test_extract_emails_from_header():
    raw = "Alice <a@acme.com>, bob@Beta.com, ella@fridgechannels.com"
    assert extract_emails_from_header_value(raw) == [
        "a@acme.com",
        "bob@beta.com",
        "ella@fridgechannels.com",
    ]


def test_collect_external_emails_strips_internal():
    emails = collect_external_emails(
        from_email="buyer@acme.com",
        to_emails=["ella@fridgechannels.com", "cc@partner.org"],
        cc_emails=["mark@fridgechannels.com"],
        internal_domains={"fridgechannels.com"},
    )
    assert emails == ["cc@partner.org", "buyer@acme.com"]


def test_extract_domains_from_client_rich_text():
    props = {
        "Domain": {
            "type": "rich_text",
            "rich_text": [{"plain_text": "acme.com, beta.io"}],
        }
    }
    assert extract_domains_from_client(props) == ["acme.com", "beta.io"]


def test_domain_cache_lookup(tmp_path):
    cache = FollowUpClientDomainCache(path=tmp_path / "domains.json")
    cache._data = {
        "updated_at": "t",
        "by_domain": {"acme.com": ["fc-1", "fc-2"]},
        "pages": {},
    }
    assert cache.lookup("www.acme.com") == ["fc-1", "fc-2"]
    assert cache.lookup("gmail.com") == []


def test_inbound_portal_payload_with_client(monkeypatch):
    monkeypatch.setattr(
        settings,
        "inbound_webhook_url",
        "https://followup-portal.fridgechannels.com/api/inbound",
    )
    monkeypatch.setattr(settings, "reply_webhook_token", "tok")
    client = InboundPortalClient()
    captured = {}

    class FakeResp:
        status_code = 200
        text = '{"ok":true}'

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResp()

    import channel_orchestrator.inbound_portal as mod

    monkeypatch.setattr(mod.httpx, "Client", FakeHttp)
    result = client.post_inbound(
        channel="Email",
        content="hello",
        sender="buyer@acme.com",
        subject="Magnet",
        followup_client_id="fc-page-1",
    )
    assert result["ok"] is True
    assert captured["url"].endswith("/api/inbound")
    assert captured["json"] == {
        "channel": "Email",
        "content": "hello",
        "sender": "buyer@acme.com",
        "object": "Magnet",
        "FollowUpClientId": "fc-page-1",
    }
    assert "taskId" not in captured["json"]


def test_inbound_portal_sender_only(monkeypatch):
    monkeypatch.setattr(
        settings,
        "inbound_webhook_url",
        "https://followup-portal.fridgechannels.com/api/inbound",
    )
    client = InboundPortalClient()
    captured = {}

    class FakeResp:
        status_code = 200
        text = "ok"

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["json"] = json
            return FakeResp()

    import channel_orchestrator.inbound_portal as mod

    monkeypatch.setattr(mod.httpx, "Client", FakeHttp)
    client.post_inbound(
        channel="Email",
        content="hi",
        sender="x@gmail.com",
        subject="Re: hi",
        followup_client_id=None,
    )
    assert "FollowUpClientId" not in captured["json"]


def test_cold_inbound_multi_client(tmp_path, monkeypatch):
    from channel_orchestrator.email_cold_inbound import handle_email_cold_inbound

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "email_internal_domains", "fridgechannels.com")
    cache = FollowUpClientDomainCache(path=tmp_path / "d.json")
    cache._data = {
        "by_domain": {"acme.com": ["fc-a", "fc-b"]},
        "pages": {},
        "updated_at": None,
    }
    portal = MagicMock()
    portal.post_inbound.return_value = {"ok": True}
    result = handle_email_cold_inbound(
        {
            "sender": "buyer@acme.com",
            "subject": "Inquiry",
            "body": "We need magnets",
            "to_emails": ["ella@fridgechannels.com"],
            "cc_emails": [],
        },
        portal=portal,
        domain_cache=cache,
    )
    assert result["cold"] is True
    assert portal.post_inbound.call_count == 2
    ids = {
        c.kwargs.get("followup_client_id")
        for c in portal.post_inbound.call_args_list
    }
    assert ids == {"fc-a", "fc-b"}


def test_cold_inbound_public_domain_sender_only(tmp_path, monkeypatch):
    from channel_orchestrator.email_cold_inbound import handle_email_cold_inbound

    monkeypatch.setattr(settings, "email_internal_domains", "fridgechannels.com")
    cache = FollowUpClientDomainCache(path=tmp_path / "d.json")
    cache._data = {"by_domain": {"acme.com": ["fc-a"]}, "pages": {}, "updated_at": None}
    portal = MagicMock()
    portal.post_inbound.return_value = {"ok": True}
    result = handle_email_cold_inbound(
        {
            "sender": "person@gmail.com",
            "subject": "Hi",
            "body": "hello",
            "to_emails": ["ella@fridgechannels.com"],
        },
        portal=portal,
        domain_cache=cache,
    )
    assert portal.post_inbound.call_count == 1
    assert portal.post_inbound.call_args.kwargs["followup_client_id"] is None
    assert portal.post_inbound.call_args.kwargs["sender"] == "person@gmail.com"
    assert result["calls"][0]["FollowUpClientId"] is None


def test_cold_inbound_internal_only_skips(tmp_path, monkeypatch):
    from channel_orchestrator.email_cold_inbound import handle_email_cold_inbound

    monkeypatch.setattr(settings, "email_internal_domains", "fridgechannels.com")
    portal = MagicMock()
    result = handle_email_cold_inbound(
        {
            "sender": "mark@fridgechannels.com",
            "subject": "fwd",
            "body": "note",
            "to_emails": ["ella@fridgechannels.com"],
            "cc_emails": ["billy@fridgechannels.com"],
        },
        portal=portal,
        domain_cache=FollowUpClientDomainCache(path=tmp_path / "d.json"),
    )
    assert result.get("skipped") is True
    portal.post_inbound.assert_not_called()


def test_email_task_match_skips_cold(tmp_path, monkeypatch):
    from channel_orchestrator.cache import OutboundCache
    from channel_orchestrator.inbound import handle_inbound_email

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    cache = OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")
    cache.set_ready(
        "a@acme.com",
        task_page_id="task-1",
        thread_id="THR-1",
        contact_page_id="c",
        conversation_page_id="v",
        extra={"gmail_thread_id": "gt-1", "email": "a@acme.com"},
    )
    wh = MagicMock()
    wh.build_email_payload.return_value = {"channel": "Email"}
    wh.post_reply.return_value = {"ok": True}
    cold_mod = MagicMock()
    monkeypatch.setattr(
        "channel_orchestrator.email_cold_inbound.handle_email_cold_inbound",
        cold_mod,
    )
    result = handle_inbound_email(
        {
            "sender": "a@acme.com",
            "body": "reply",
            "subject": "Re",
            "gmail_thread_id": "gt-1",
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is True
    assert result.get("cold") is not True
    wh.post_reply.assert_called_once()
    cold_mod.assert_not_called()


def test_email_unmatched_goes_cold(tmp_path, monkeypatch):
    from channel_orchestrator.cache import OutboundCache
    from channel_orchestrator.inbound import handle_inbound_email

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notion_token", "")
    monkeypatch.setattr(settings, "email_internal_domains", "fridgechannels.com")
    cache = OutboundCache(channel="EMAIL", path=tmp_path / "email_cache.json")
    portal = MagicMock()
    portal.post_inbound.return_value = {"ok": True}
    monkeypatch.setattr(
        "channel_orchestrator.email_cold_inbound.InboundPortalClient",
        lambda: portal,
    )
    domain_cache = FollowUpClientDomainCache(path=tmp_path / "domains.json")
    domain_cache._data = {
        "by_domain": {"acme.com": ["fc-1"]},
        "pages": {},
        "updated_at": None,
    }
    monkeypatch.setattr(
        "channel_orchestrator.email_cold_inbound.get_domain_cache",
        lambda: domain_cache,
    )
    wh = MagicMock()
    result = handle_inbound_email(
        {
            "sender": "buyer@acme.com",
            "body": "cold lead",
            "subject": "Magnet",
            "to_emails": ["ella@fridgechannels.com"],
        },
        notion=MagicMock(),
        cache=cache,
        reply_webhook=wh,
        write_notion=False,
    )
    assert result["matched"] is False
    assert result["cold"] is True
    wh.post_reply.assert_not_called()
    portal.post_inbound.assert_called_once()
    assert portal.post_inbound.call_args.kwargs["followup_client_id"] == "fc-1"
