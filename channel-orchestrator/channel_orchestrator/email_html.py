from __future__ import annotations

from html.parser import HTMLParser
from html import escape, unescape
from typing import Callable
from urllib.parse import urlparse
import re

ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "a",
        "ul",
        "ol",
        "li",
        "img",
        "h1",
        "h2",
        "h3",
        "blockquote",
        "div",
        "span",
    }
)
VOID_TAGS = frozenset({"br", "img"})
DROP_WITH_CONTENT = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "link",
        "meta",
        "base",
        "form",
        "textarea",
        "svg",
        "math",
        "noscript",
    }
)
HTML_TAG_RE = re.compile(
    r"<(p|div|br\s*/?|img|strong|em|b|i|u|ul|ol|li|a|h[1-3]|blockquote)\b",
    re.I,
)
BASE64_MEDIA_RE = re.compile(r"data:(image|video|application)/[a-z0-9.+-]+;base64,", re.I)
IMG_SRC_RE = re.compile(r"<img\b[^>]*\bsrc=['\"]([^'\"]+)['\"][^>]*>", re.I)


def looks_like_html(value: str | None) -> bool:
    return bool(HTML_TAG_RE.search(value or ""))


def contains_inline_base64(value: str | None) -> bool:
    return bool(BASE64_MEDIA_RE.search(value or ""))


def is_safe_href(url: str) -> bool:
    text = (url or "").strip()
    lower = text.lower()
    if not text:
        return False
    if lower.startswith(("javascript:", "data:", "vbscript:")):
        return False
    return lower.startswith("https://") or lower.startswith("mailto:")


class _Sanitizer(HTMLParser):
    def __init__(self, allow_image_url: Callable[[str], bool]) -> None:
        super().__init__(convert_charrefs=True)
        self.allow_image_url = allow_image_url
        self.out: list[str] = []
        self._drop: str | None = None
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop:
            if tag == self._drop:
                self._drop_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            self._drop = tag
            self._drop_depth = 1
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
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop or tag in DROP_WITH_CONTENT:
            return
        rendered = self._open(tag, attrs)
        if rendered:
            self.out.append(rendered)

    def handle_data(self, data: str) -> None:
        if self._drop or not data:
            return
        self.out.append(escape(data, quote=True))

    def handle_comment(self, data: str) -> None:
        return

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        if tag not in ALLOWED_TAGS:
            return ""
        mapping = {k.lower(): (v or "") for k, v in attrs}
        kept: list[str] = []
        if tag == "img":
            src = mapping.get("src", "").strip()
            if not src or not self.allow_image_url(src):
                return None
            kept.append(f'src="{escape(src, quote=True)}"')
            alt = mapping.get("alt", "").strip()
            if alt:
                kept.append(f'alt="{escape(alt, quote=True)}"')
            kept.append('style="max-width:100%;height:auto"')
        elif tag == "a":
            href = mapping.get("href", "").strip()
            if href and is_safe_href(href):
                kept.append(f'href="{escape(href, quote=True)}"')
                kept.append('rel="noopener noreferrer"')
        attr = (" " + " ".join(kept)) if kept else ""
        if tag in VOID_TAGS:
            return f"<{tag}{attr} />"
        return f"<{tag}{attr}>"


def sanitize_email_html(html: str, allow_image_url: Callable[[str], bool]) -> str:
    parser = _Sanitizer(allow_image_url)
    parser.feed(html or "")
    parser.close()
    return "".join(parser.out).strip()


def html_to_plain_text(html: str) -> str:
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", html or "", flags=re.I)
    text = re.sub(r"<\s*/\s*(p|div|li|h1|h2|h3|blockquote)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*img\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_img_srcs(html: str) -> list[str]:
    return [m.group(1) for m in IMG_SRC_RE.finditer(html or "")]


def rewrite_img_src(html: str, old: str, new: str) -> str:
    """Replace one img src value without touching other attributes."""

    def repl(match: re.Match[str]) -> str:
        src = match.group(1)
        if src != old:
            return match.group(0)
        return match.group(0).replace(src, new, 1)

    return IMG_SRC_RE.sub(repl, html or "")


def mime_from_url_or_header(url: str, content_type: str | None) -> tuple[str, str]:
    raw = (content_type or "").split(";")[0].strip().lower()
    if raw.startswith("image/") and "/" in raw:
        main, sub = raw.split("/", 1)
        return main, sub
    path = urlparse(url).path.lower()
    if path.endswith(".png"):
        return "image", "png"
    if path.endswith(".webp"):
        return "image", "webp"
    if path.endswith(".gif"):
        return "image", "gif"
    return "image", "jpeg"
