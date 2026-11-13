"""Gmail-style quoted history ("On … wrote:") for replies sent inside an existing thread."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from html import escape
from html.parser import HTMLParser
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .email_html import html_to_plain_text

QUOTE_TAGS = frozenset(
    {
        "a", "abbr", "address", "b", "big", "blockquote", "br", "caption", "center", "cite",
        "code", "col", "colgroup", "dd", "del", "div", "dl", "dt", "em", "figcaption", "figure",
        "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "li", "mark", "ol",
        "p", "pre", "q", "s", "small", "span", "strike", "strong", "sub", "sup", "table", "tbody",
        "td", "tfoot", "th", "thead", "tr", "tt", "u", "ul", "wbr",
    }
)
QUOTE_VOID_TAGS = frozenset({"br", "col", "hr", "img", "wbr"})
QUOTE_DROP_WITH_CONTENT = frozenset(
    {
        "script", "style", "iframe", "object", "embed", "link", "meta", "base", "head", "title",
        "form", "input", "button", "select", "textarea", "svg", "math", "noscript", "template",
    }
)
QUOTE_ATTRS = frozenset(
    {
        "align", "alt", "bgcolor", "border", "cellpadding", "cellspacing", "class", "color",
        "colspan", "dir", "face", "height", "rowspan", "size", "start", "style", "title", "type",
        "valign", "width",
    }
)
_UNSAFE_STYLE_RE = re.compile(r"expression\s*\(|javascript:|vbscript:|@import|behavior\s*:|url\s*\(", re.I)
_TRACKING_PIXEL_RE = re.compile(r"/api/track/open/", re.I)
_CID_REF_RE = re.compile(r"cid:([^\"'\s>)]+)", re.I)

GMAIL_QUOTE_STYLE = "margin:0px 0px 0px 0.8ex;border-left:1px solid rgb(204,204,204);padding-left:1ex"


@dataclass
class ReplyQuote:
    html: str
    plain: str
    related: list[dict[str, Any]] = field(default_factory=list)


class _QuoteSanitizer(HTMLParser):
    """Looser than the body sanitizer: keeps tables / inline styles so the quote looks like the original."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._drop: str | None = None
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop:
            if tag == self._drop:
                self._drop_depth += 1
            return
        if tag in QUOTE_DROP_WITH_CONTENT:
            self._drop = tag
            self._drop_depth = 1
            return
        rendered = self._open(tag, attrs)
        if rendered:
            self.out.append(rendered)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop or tag in QUOTE_DROP_WITH_CONTENT:
            return
        rendered = self._open(tag, attrs)
        if rendered:
            self.out.append(rendered)

    def handle_endtag(self, tag: str) -> None:
        if self._drop:
            if tag == self._drop:
                self._drop_depth -= 1
                if self._drop_depth <= 0:
                    self._drop = None
                    self._drop_depth = 0
            return
        if tag in QUOTE_TAGS and tag not in QUOTE_VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._drop or not data:
            return
        self.out.append(escape(data, quote=False))

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        if tag not in QUOTE_TAGS:
            return None
        mapping = {k.lower(): (v or "") for k, v in attrs}
        kept: list[str] = []
        if tag == "img":
            src = mapping.get("src", "").strip()
            lower = src.lower()
            if not lower.startswith(("https://", "http://", "cid:")):
                return None
            # Our own earlier sends carry an open pixel; re-sending it would log fake opens.
            if _TRACKING_PIXEL_RE.search(src):
                return None
            kept.append(f'src="{escape(src, quote=True)}"')
        elif tag == "a":
            href = mapping.get("href", "").strip()
            if href.lower().startswith(("https://", "http://", "mailto:")):
                kept.append(f'href="{escape(href, quote=True)}"')
                kept.append('target="_blank"')
        for key, value in mapping.items():
            if key not in QUOTE_ATTRS or not value:
                continue
            if key == "style" and _UNSAFE_STYLE_RE.search(value):
                continue
            kept.append(f'{key}="{escape(value, quote=True)}"')
        attr = (" " + " ".join(kept)) if kept else ""
        return f"<{tag}{attr}>"


def sanitize_quoted_html(html: str) -> str:
    parser = _QuoteSanitizer()
    parser.feed(html or "")
    parser.close()
    return "".join(parser.out).strip()


def plain_to_quote_html(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "<br>".join(escape(line, quote=False) for line in lines)


def format_attribution(from_header: str, sent_at: datetime | None, tz_name: str) -> tuple[str, str]:
    """Gmail's `On Fri, Nov 13, 2026 at 5:00 AM Eric Hatch <eric@x.com> wrote:` as (html, plain)."""
    name, addr = parseaddr(from_header or "")
    name = name.strip().strip('"')
    addr = addr.strip()
    if sent_at is not None:
        try:
            local = sent_at.astimezone(ZoneInfo(tz_name))
        except Exception:  # noqa: BLE001
            local = sent_at
        hour = local.hour % 12 or 12
        ampm = "AM" if local.hour < 12 else "PM"
        when_text = f"On {local:%a}, {local:%b} {local.day}, {local.year} at {hour}:{local:%M}\u202f{ampm} "
    else:
        when_text = "On "
    if addr:
        sender_html = f'&lt;<a href="mailto:{escape(addr, quote=True)}">{escape(addr)}</a>&gt;'
        sender_plain = f"<{addr}>"
        if name:
            sender_html = f"{escape(name)} {sender_html}"
            sender_plain = f"{name} {sender_plain}"
    else:
        sender_html = escape(from_header or "")
        sender_plain = from_header or ""
    return (
        f"{escape(when_text)}{sender_html} wrote:",
        f"{when_text}{sender_plain} wrote:",
    )


def _headers(part: dict[str, Any]) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in part.get("headers") or []
        if h.get("name") and h.get("value") is not None
    }


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _walk(part: dict[str, Any]):
    yield part
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            yield from _walk(child)


def _body_text(part: dict[str, Any], fetch_attachment: Callable[[str], bytes]) -> str:
    body = part.get("body") or {}
    raw = b""
    if body.get("data"):
        raw = _b64url(body["data"])
    elif body.get("attachmentId"):
        raw = fetch_attachment(body["attachmentId"])
    return raw.decode("utf-8", errors="replace")


def _inner_body(html: str) -> str:
    match = re.search(r"<body\b[^>]*>(.*)</body\s*>", html or "", re.I | re.S)
    return match.group(1) if match else (html or "")


def build_reply_quote(
    message: dict[str, Any],
    fetch_attachment: Callable[[str], bytes],
    *,
    tz_name: str,
    max_inline_bytes: int,
) -> ReplyQuote | None:
    """Quote block for a Gmail API `format=full` message, plus its cid images re-keyed for re-attach."""
    payload = message.get("payload") or {}
    html_part = next((p for p in _walk(payload) if p.get("mimeType") == "text/html" and not p.get("filename")), None)
    plain_part = next((p for p in _walk(payload) if p.get("mimeType") == "text/plain" and not p.get("filename")), None)
    html_src = _body_text(html_part, fetch_attachment) if html_part else ""
    plain_src = _body_text(plain_part, fetch_attachment) if plain_part else ""
    if html_src.strip():
        inner = sanitize_quoted_html(_inner_body(html_src))
    elif plain_src.strip():
        inner = plain_to_quote_html(plain_src.strip())
    else:
        return None
    if not inner.strip():
        return None

    related: list[dict[str, Any]] = []
    referenced = {m.group(1) for m in _CID_REF_RE.finditer(inner)}
    renamed: dict[str, str] = {}
    for part in _walk(payload):
        headers = _headers(part)
        content_id = (headers.get("content-id") or headers.get("x-attachment-id") or "").strip().strip("<>")
        if not content_id or content_id not in referenced or content_id in renamed:
            continue
        mime = (part.get("mimeType") or "").lower()
        if not mime.startswith("image/"):
            continue
        body = part.get("body") or {}
        if int(body.get("size") or 0) > max_inline_bytes:
            continue
        try:
            data = _b64url(body["data"]) if body.get("data") else fetch_attachment(body.get("attachmentId") or "")
        except Exception:  # noqa: BLE001
            continue
        if not data or len(data) > max_inline_bytes:
            continue
        # Our own body already uses img1, img2…; prefix so a quoted cid can never collide.
        new_cid = f"quote{len(renamed) + 1}"
        renamed[content_id] = new_cid
        main, _, sub = mime.partition("/")
        related.append({"cid": new_cid, "bytes": data, "maintype": main, "subtype": sub or "png"})
    for old, new in renamed.items():
        inner = re.sub(rf"cid:{re.escape(old)}(?=[\"'\s>)]|$)", f"cid:{new}", inner, flags=re.I)

    headers = _headers(payload)
    sent_at: datetime | None = None
    if message.get("internalDate"):
        try:
            sent_at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            sent_at = None
    attr_html, attr_plain = format_attribution(headers.get("from") or "", sent_at, tz_name)

    html = (
        '<br><div class="gmail_quote gmail_quote_container">'
        f'<div dir="ltr" class="gmail_attr">{attr_html}<br></div>'
        f'<blockquote class="gmail_quote" style="{GMAIL_QUOTE_STYLE}">{inner}</blockquote>'
        "</div>"
    )
    quoted_text = plain_src.strip() if plain_src.strip() else html_to_plain_text(inner)
    plain = attr_plain + "\n" + "\n".join(f"> {line}" if line else ">" for line in quoted_text.splitlines())
    return ReplyQuote(html=html, plain=plain, related=related)
