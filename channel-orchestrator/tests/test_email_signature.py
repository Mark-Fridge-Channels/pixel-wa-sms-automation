"""Hommio outbound email signature."""

from __future__ import annotations

import base64
from email import message_from_bytes

from channel_orchestrator.config import settings
from channel_orchestrator.email_signature import (
    append_signature_html,
    signature_already_present,
    signature_html,
)
from channel_orchestrator.gmail_client import GmailClient

LOGO = (
    "https://amzn-s3-fc-bucket.s3.sa-east-1.amazonaws.com/"
    "images/hommio-email-signature-logo.png"
)


def test_signature_html_contains_brand_copy(monkeypatch):
    monkeypatch.setattr(settings, "email_signature_rebrand_note", "FridgeChannels is now Hommio")
    html = signature_html()
    assert "hommio-email-signature" in html
    assert "Ella" in html
    assert "Account Manager" in html
    assert "FridgeChannels is now Hommio" in html
    assert "Retention is at home" in html
    assert "hommio.ai" in html
    assert LOGO in html
    # Compact layout: identity / pitch / footer (not six stacked rows).
    assert html.count("<tr>") <= 5


def test_signature_rebrand_note_can_hide(monkeypatch):
    monkeypatch.setattr(settings, "email_signature_rebrand_note", "")
    html = signature_html()
    assert "FridgeChannels is now Hommio" not in html
    assert "Account Manager" in html
    assert "hommio.ai" in html


def test_append_signature_idempotent():
    once = append_signature_html("<p>Hi</p>")
    twice = append_signature_html(once)
    assert once.count('data-hommio-email-signature="1"') == 1
    assert twice == once
    assert signature_already_present(once)


def test_send_message_appends_signature_to_plain(monkeypatch):
    monkeypatch.setattr(settings, "email_signature_enabled", True)
    monkeypatch.setattr(settings, "email_signature_logo_url", LOGO)
    monkeypatch.setattr(settings, "s3_bucket", "amzn-s3-fc-bucket")
    monkeypatch.setattr(settings, "aws_region", "sa-east-1")
    monkeypatch.setattr(settings, "s3_public_base_url", "")
    monkeypatch.setattr(settings, "email_attachment_max_bytes", 10 * 1024 * 1024)

    client = GmailClient()
    captured: dict = {}

    def fake_request(method, path, **kwargs):
        captured["json"] = kwargs.get("json")
        return {"id": "m1", "threadId": "t1"}

    class FakeResp:
        status_code = 200
        content = b"\x89PNG"
        headers = {"content-type": "image/png"}

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            assert url == LOGO
            return FakeResp()

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr("channel_orchestrator.gmail_client.httpx.Client", FakeHttp)

    client.send_message(
        to="oh.duang@gmail.com",
        subject="FollowUp signature test",
        body="Hi there,\n\nThanks for the conversation earlier.",
    )
    raw = captured["json"]["raw"] + "=="
    msg = message_from_bytes(base64.urlsafe_b64decode(raw))
    html = next(
        part.get_payload(decode=True).decode("utf-8", errors="replace")
        for part in msg.walk()
        if part.get_content_type() == "text/html"
    )
    assert "Thanks for the conversation earlier" in html
    assert "Ella" in html
    assert "Account Manager" in html
    assert "Retention is at home" in html
    assert "cid:" in html.lower()
    assert "hommio-email-signature" in html


def test_send_message_signature_can_disable(monkeypatch):
    monkeypatch.setattr(settings, "email_signature_enabled", False)
    client = GmailClient()
    captured: dict = {}

    def fake_request(method, path, **kwargs):
        captured["json"] = kwargs.get("json")
        return {"id": "m1", "threadId": "t1"}

    monkeypatch.setattr(client, "_request", fake_request)
    client.send_message(to="a@b.com", subject="Hi", body="Plain only")
    decoded = base64.urlsafe_b64decode(captured["json"]["raw"] + "==").decode(
        "utf-8", errors="replace"
    )
    assert "Plain only" in decoded
    assert "Account Manager" not in decoded
    assert "text/html" not in decoded.lower()
