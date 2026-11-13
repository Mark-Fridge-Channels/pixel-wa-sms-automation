"""Gmail-style quoted history on thread replies."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email import message_from_bytes

from channel_orchestrator.config import settings
from channel_orchestrator.email_quote import build_reply_quote, format_attribution, sanitize_quoted_html
from channel_orchestrator.gmail_client import GmailClient


def _b64(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# Fri, Nov 13, 2026 10:00 UTC == 5:00 AM New York
INTERNAL_MS = str(int(datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc).timestamp() * 1000))

HTML_ORIGINAL = (
    "<html><head><style>p{color:red}</style></head><body>"
    '<div dir="ltr">Got it, that all makes sense to me!<br><br>Thank you!</div>'
    '<img src="cid:logo@x" width="40">'
    '<img src="https://portal.example.com/api/track/open/eo1.abc">'
    "<script>alert(1)</script>"
    '<div class="gmail_quote"><blockquote class="gmail_quote">Earlier <b>history</b></blockquote></div>'
    "</body></html>"
)


def _full_message() -> dict:
    return {
        "id": "gm-in",
        "internalDate": INTERNAL_MS,
        "payload": {
            "mimeType": "multipart/related",
            "headers": [{"name": "From", "value": "Eric Hatch <eric@mayatea.com>"}],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("Got it, that all makes sense to me!\n\nThank you!\n\n> Earlier history")}},
                        {"mimeType": "text/html", "body": {"data": _b64(HTML_ORIGINAL)}},
                    ],
                },
                {
                    "mimeType": "image/png",
                    "filename": "logo.png",
                    "headers": [{"name": "Content-ID", "value": "<logo@x>"}],
                    "body": {"attachmentId": "att-1", "size": 4},
                },
            ],
        },
    }


def test_format_attribution_matches_gmail():
    html, plain = format_attribution(
        "Eric Hatch <eric@mayatea.com>",
        datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc),
        "America/New_York",
    )
    assert plain == "On Fri, Nov 13, 2026 at 5:00\u202fAM Eric Hatch <eric@mayatea.com> wrote:"
    assert 'Eric Hatch &lt;<a href="mailto:eric@mayatea.com">eric@mayatea.com</a>&gt; wrote:' in html
    _, own = format_attribution("ella@fridgechannels.com", None, "America/New_York")
    assert own == "On <ella@fridgechannels.com> wrote:"


def test_sanitize_quoted_html_keeps_layout_drops_active_content():
    html = sanitize_quoted_html(
        '<table style="width:100%" onclick="x()"><tr><td bgcolor="#fff">A</td></tr></table>'
        '<a href="javascript:alert(1)">bad</a><a href="https://ok.example">ok</a>'
        '<p style="background:url(https://evil/x)">s</p><form><input></form>'
    )
    assert '<table style="width:100%">' in html
    assert 'bgcolor="#fff"' in html
    assert "onclick" not in html
    assert "javascript:" not in html
    assert 'href="https://ok.example"' in html
    assert "url(" not in html
    assert "<form" not in html and "<input" not in html


def test_build_reply_quote_html_with_inline_image():
    fetched: list[str] = []

    def fetch(attachment_id: str) -> bytes:
        fetched.append(attachment_id)
        return b"\x89PNG"

    quote = build_reply_quote(_full_message(), fetch, tz_name="America/New_York", max_inline_bytes=1024)
    assert quote is not None
    assert quote.html.startswith('<br><div class="gmail_quote gmail_quote_container">')
    assert "On Fri, Nov 13, 2026 at 5:00\u202fAM Eric Hatch" in quote.html
    assert 'blockquote class="gmail_quote" style="margin:0px 0px 0px 0.8ex;' in quote.html
    assert "Got it, that all makes sense to me!<br><br>Thank you!" in quote.html
    assert "Earlier <b>history</b>" in quote.html
    assert "<script" not in quote.html and "alert" not in quote.html
    assert "<style" not in quote.html and "color:red" not in quote.html
    assert "/api/track/open/" not in quote.html
    assert 'src="cid:quote1"' in quote.html
    assert fetched == ["att-1"]
    assert quote.related == [{"cid": "quote1", "bytes": b"\x89PNG", "maintype": "image", "subtype": "png"}]
    assert quote.plain.splitlines()[0].endswith("Eric Hatch <eric@mayatea.com> wrote:")
    assert "> Got it, that all makes sense to me!" in quote.plain
    assert "> > Earlier history" in quote.plain


def test_build_reply_quote_plain_only():
    message = {
        "internalDate": INTERNAL_MS,
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": "eric@mayatea.com"}],
            "body": {"data": _b64("Line 1\n\nA < B")},
        },
    }
    quote = build_reply_quote(message, lambda _id: b"", tz_name="America/New_York", max_inline_bytes=1024)
    assert quote is not None
    assert "Line 1<br><br>A &lt; B" in quote.html
    assert quote.related == []


def _fake_gmail(monkeypatch, client: GmailClient) -> dict:
    captured: dict = {}

    def fake_request(method, path, **kwargs):
        if method == "GET" and path == "/threads/gt-1":
            return {"messages": [{"id": "gm-in", "labelIds": ["INBOX"]}, {"id": "gm-draft", "labelIds": ["DRAFT"]}]}
        if method == "GET" and path == "/messages/gm-in":
            if (kwargs.get("params") or {}).get("format") == "metadata":
                return {
                    "id": "gm-in",
                    "payload": {
                        "headers": [
                            {"name": "Message-ID", "value": "<abc@mail.gmail.com>"},
                            {"name": "Subject", "value": "Maya magnet"},
                        ]
                    },
                }
            return _full_message()
        if method == "GET" and path == "/messages/gm-in/attachments/att-1":
            return {"data": _b64(b"\x89PNG")}
        if method == "POST" and path == "/messages/send":
            captured["json"] = kwargs.get("json")
            return {"id": "gm-out", "threadId": "gt-1"}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(client, "_request", fake_request)
    return captured


def _sent_html(captured: dict) -> tuple[str, list]:
    msg = message_from_bytes(base64.urlsafe_b64decode(captured["json"]["raw"] + "=="))
    html = next(
        part.get_payload(decode=True).decode("utf-8", errors="replace")
        for part in msg.walk()
        if part.get_content_type() == "text/html"
    )
    return html, list(msg.walk())


def test_send_reply_appends_gmail_quote(monkeypatch):
    monkeypatch.setattr(settings, "email_signature_enabled", False)
    monkeypatch.setattr(settings, "email_attachment_max_bytes", 1024 * 1024)
    client = GmailClient()
    captured = _fake_gmail(monkeypatch, client)

    result = client.send_new_or_reply(
        to="eric@mayatea.com",
        subject="Maya magnet",
        body="<div>Thanks Eric,</div><div><br /></div><div>Talk soon</div>",
        gmail_thread_id="gt-1",
    )
    assert result["quoted"] is True
    html, parts = _sent_html(captured)
    assert html.index("Talk soon") < html.index('class="gmail_quote')
    assert "Eric Hatch" in html
    assert 'src="cid:quote1"' in html
    assert any(p.get("Content-ID") == "<quote1>" for p in parts)
    assert captured["json"]["threadId"] == "gt-1"


def test_send_reply_without_quote(monkeypatch):
    monkeypatch.setattr(settings, "email_signature_enabled", False)
    client = GmailClient()
    captured = _fake_gmail(monkeypatch, client)

    result = client.send_new_or_reply(
        to="eric@mayatea.com",
        subject="Maya magnet",
        body="<div>Thanks Eric</div>",
        gmail_thread_id="gt-1",
        quote_history=False,
    )
    assert result["quoted"] is False
    html, _ = _sent_html(captured)
    assert "gmail_quote" not in html


def test_plain_body_reply_still_gets_html_quote(monkeypatch):
    monkeypatch.setattr(settings, "email_signature_enabled", False)
    monkeypatch.setattr(settings, "email_attachment_max_bytes", 1024 * 1024)
    client = GmailClient()
    captured = _fake_gmail(monkeypatch, client)

    client.send_new_or_reply(to="eric@mayatea.com", subject="Re", body="Plain thanks", gmail_thread_id="gt-1")
    html, _ = _sent_html(captured)
    assert html.startswith("Plain thanks")
    assert "gmail_quote" in html


def test_quote_failure_does_not_block_send(monkeypatch):
    monkeypatch.setattr(settings, "email_signature_enabled", False)
    client = GmailClient()
    captured = _fake_gmail(monkeypatch, client)

    def boom(_message_id):
        raise RuntimeError("Gmail 500")

    monkeypatch.setattr(client, "reply_quote", boom)
    result = client.send_new_or_reply(
        to="eric@mayatea.com", subject="Re", body="<div>Hi</div>", gmail_thread_id="gt-1"
    )
    assert result["quoted"] is False
    assert "Gmail 500" in result["quote_warning"]
    assert captured["json"]["threadId"] == "gt-1"
