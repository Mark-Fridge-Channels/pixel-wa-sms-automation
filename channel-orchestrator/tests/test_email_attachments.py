"""Email attachments + CC helpers."""

from __future__ import annotations

from channel_orchestrator.email_attachments import (
    append_content_notices,
    kind_from_mime,
    parse_attachments_json,
    parse_cc_list,
    validate_attachment_bytes,
    validate_attachment_meta,
)
from channel_orchestrator.reply_webhook import ReplyWebhookClient
from channel_orchestrator.inbound_portal import InboundPortalClient
from channel_orchestrator.config import settings


def test_kind_from_mime():
    assert kind_from_mime("image/png") == "image"
    assert kind_from_mime("application/pdf") == "file"
    assert kind_from_mime("video/mp4") == "video"


def test_parse_cc_list():
    assert parse_cc_list("a@x.com, b@Y.com; c@x.com") == ["a@x.com", "b@y.com", "c@x.com"]
    assert parse_cc_list("") == []


def test_parse_attachments_json():
    raw = '[{"id":"abc","kind":"file","name":"q.pdf","mimeType":"application/pdf","size":12,"url":"https://x/files/abc.pdf"}]'
    items = parse_attachments_json(raw)
    assert len(items) == 1
    assert items[0]["name"] == "q.pdf"


def test_validate_video_mime():
    assert validate_attachment_bytes(filename="a.mp4", mime="video/mp4", size=100) is None
    assert validate_attachment_bytes(filename="a.mov", mime="video/quicktime", size=100) is None
    assert validate_attachment_bytes(filename="a.3gp", mime="video/3gpp", size=100) is None
    ok, reason = validate_attachment_meta(
        {
            "id": "vid",
            "kind": "video",
            "name": "clip.mov",
            "mimeType": "video/quicktime",
            "size": 100,
            "url": "https://bucket.s3.us-east-1.amazonaws.com/videos/vid.mov",
        }
    )
    assert reason is None
    assert ok and ok["kind"] == "video"


def test_validate_bad_mime():
    reason = validate_attachment_bytes(
        filename="x.exe",
        mime="application/x-msdownload",
        size=100,
    )
    assert reason and "MIME" in reason


def test_append_notices():
    assert "附件未入库" in append_content_notices("hi", ["x.pdf（超过 10MB）"])
    assert append_content_notices("", ["only"]).startswith("【附件未入库")


def test_reply_payload_includes_attachments(monkeypatch):
    monkeypatch.setattr(settings, "reply_webhook_url", "https://example.com/api/replies")
    client = ReplyWebhookClient()
    payload = client.build_email_payload(
        task_id="t1",
        thread_id="THR-1",
        content="hi",
        sender="a@b.com",
        occurred_at="2026-09-22T00:00:00Z",
        subject="Re: x",
        attachments=[
            {
                "id": "abc",
                "kind": "file",
                "name": "q.pdf",
                "mimeType": "application/pdf",
                "size": 10,
                "url": "https://bucket.s3.us-east-1.amazonaws.com/files/abc.pdf",
            }
        ],
    )
    assert payload["attachments"][0]["id"] == "abc"

    captured = {}

    class FakeResp:
        status_code = 201
        text = '{"ok":true}'

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

    import channel_orchestrator.reply_webhook as mod

    monkeypatch.setattr(mod.httpx, "Client", FakeHttp)
    client.post_reply(payload)
    assert captured["json"]["attachments"][0]["name"] == "q.pdf"


def test_inbound_portal_attachments_and_empty_content(monkeypatch):
    monkeypatch.setattr(
        settings,
        "inbound_webhook_url",
        "https://followup-portal.fridgechannels.com/api/inbound",
    )
    client = InboundPortalClient()
    captured = {}

    class FakeResp:
        status_code = 201
        text = '{"conversationId":"c1"}'

        def json(self):
            return {"conversationId": "c1"}

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
        content="",
        sender="buyer@acme.com",
        subject="docs",
        attachments=[
            {
                "id": "abc",
                "kind": "file",
                "name": "q.pdf",
                "mimeType": "application/pdf",
                "size": 10,
                "url": "https://bucket.s3.us-east-1.amazonaws.com/files/abc.pdf",
            }
        ],
    )
    assert captured["json"]["content"] == "[attachments]"
    assert captured["json"]["attachments"]


def test_outbound_meta_skips_bad_mime():
    meta, err = validate_attachment_meta(
        {
            "id": "1",
            "name": "x.exe",
            "mimeType": "application/x-msdownload",
            "size": 10,
            "url": "https://x/y",
        }
    )
    assert meta is None
    assert err and "MIME" in err
