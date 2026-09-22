"""Email HTML sanitizer + Gmail HTML/CID helpers."""

from __future__ import annotations

from channel_orchestrator.config import settings
from channel_orchestrator.email_html import (
    contains_inline_base64,
    html_to_plain_text,
    looks_like_html,
    sanitize_email_html,
)
from channel_orchestrator.gmail_client import GmailClient
from channel_orchestrator.s3_media import is_allowed_s3_media_url


S3 = "https://bucket.s3.us-east-1.amazonaws.com/images/abc.jpg"


def _allow(url: str) -> bool:
    return url.startswith("https://bucket.s3.us-east-1.amazonaws.com/")


def test_looks_like_html_compat_with_plain_text():
    assert looks_like_html("Hello 3 < 5 and mark@x.com") is False
    assert looks_like_html("<p>Hello</p>") is True
    assert contains_inline_base64("data:image/png;base64,abcd") is True


def test_sanitize_strips_xss_keeps_s3_img():
    html = sanitize_email_html(
        f'<p onclick="alert(1)">Hi <strong>Alex</strong></p>'
        f'<script>alert(1)</script>'
        f'<img src="{S3}" onerror="alert(1)">'
        f'<img src="https://evil.example/x.png">'
        f'<a href="javascript:alert(1)">x</a>',
        _allow,
    )
    assert "script" not in html
    assert "onclick" not in html
    assert "onerror" not in html
    assert "javascript:" not in html
    assert "evil.example" not in html
    assert "Hi" in html
    assert "Alex" in html
    assert S3 in html
    assert "max-width:100%" in html


def test_html_to_plain_text():
    assert html_to_plain_text("<p>Hi <strong>Alex</strong></p><p>Next</p>") == "Hi Alex\nNext"


def test_is_allowed_s3_media_url(monkeypatch):
    monkeypatch.setattr(settings, "s3_bucket", "bucket")
    monkeypatch.setattr(settings, "aws_region", "us-east-1")
    monkeypatch.setattr(settings, "s3_public_base_url", "")
    assert is_allowed_s3_media_url(S3) is True
    assert is_allowed_s3_media_url("https://evil.example/x.jpg") is False
    assert is_allowed_s3_media_url("data:image/png;base64,xx") is False


def test_send_message_plain_legacy(monkeypatch):
    client = GmailClient()
    captured: dict = {}

    def fake_request(method, path, **kwargs):
        captured["json"] = kwargs.get("json")
        return {"id": "m1", "threadId": "t1"}

    monkeypatch.setattr(client, "_request", fake_request)
    client.send_message(to="a@b.com", subject="Hi", body="Hello there")
    raw = captured["json"]["raw"] + "=="
    import base64

    decoded = base64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")
    assert "Hello there" in decoded
    assert "text/html" not in decoded.lower()


def test_send_message_html_with_cid(monkeypatch):
    monkeypatch.setattr(settings, "s3_bucket", "bucket")
    monkeypatch.setattr(settings, "aws_region", "us-east-1")
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
            assert url == S3
            return FakeResp()

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr("channel_orchestrator.gmail_client.httpx.Client", FakeHttp)

    client.send_message(
        to="a@b.com",
        subject="Catalog",
        body=f'<p>Hi</p><img src="{S3}" alt="catalog">',
    )
    import base64

    raw = captured["json"]["raw"] + "=="
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")
    assert "text/html" in decoded.lower()
    assert "cid:img1" in decoded
    assert "Content-ID:" in decoded or "content-id:" in decoded.lower()
    assert S3 not in decoded  # rewritten to cid
    assert "Hi" in decoded
    assert "alert" not in decoded
