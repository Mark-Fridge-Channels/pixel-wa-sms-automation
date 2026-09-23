from __future__ import annotations

import json
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .email_util import normalize_email
from .s3_media import S3Uploader

log = logging.getLogger(__name__)

DEFAULT_EMAIL_MIME = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/quicktime",
        "video/3gpp",
    }
)


def allowed_email_mimes() -> set[str]:
    raw = (settings.email_attachment_mime_types or "").strip()
    if not raw:
        return set(DEFAULT_EMAIL_MIME)
    return {p.strip().lower() for p in raw.split(",") if p.strip()} or set(DEFAULT_EMAIL_MIME)


def kind_from_mime(mime: str | None) -> str:
    m = (mime or "").strip().lower()
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    return "file"


def parse_cc_list(raw: str | None) -> list[str]:
    if not raw or not str(raw).strip():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in str(raw).replace(";", ",").split(","):
        e = normalize_email(part.strip())
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def parse_attachments_json(raw: str | None) -> list[dict[str, Any]]:
    """Parse Conversation Attachments rich_text JSON → list of dicts."""
    if not raw or not str(raw).strip():
        return []
    text = str(raw).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Attachments JSON invalid: %s", text[:120])
        return []
    if isinstance(data, dict) and isinstance(data.get("attachments"), list):
        data = data["attachments"]
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def format_attachment_notices(notices: list[str]) -> str:
    if not notices:
        return ""
    return "\n".join(f"【附件未入库：{n}】" for n in notices)


def append_content_notices(content: str, notices: list[str]) -> str:
    tip = format_attachment_notices(notices)
    body = (content or "").rstrip()
    if not tip:
        return body
    if not body:
        return tip
    return f"{body}\n\n{tip}"


def _ext_for_mime(mime: str, filename: str | None = None) -> str:
    if filename:
        suf = Path(filename).suffix
        if suf:
            return suf.lower()
    return {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/3gpp": ".3gp",
    }.get(mime.lower(), mimetypes.guess_extension(mime) or ".bin")


def validate_attachment_bytes(
    *,
    filename: str,
    mime: str,
    size: int,
    allowed: set[str] | None = None,
    max_bytes: int | None = None,
) -> str | None:
    """Return Chinese reason if illegal, else None."""
    allowed = allowed if allowed is not None else allowed_email_mimes()
    max_bytes = max_bytes if max_bytes is not None else int(settings.email_attachment_max_bytes)
    mime_l = (mime or "").strip().lower() or "application/octet-stream"
    name = filename or "attachment"
    if mime_l not in allowed:
        return f"{name}（MIME 不允许：{mime_l}）"
    if size <= 0:
        return f"{name}（大小无效）"
    if size > max_bytes:
        mb = max_bytes / (1024 * 1024)
        return f"{name}（超过 {mb:g}MB）"
    return None


def validate_attachment_meta(item: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Outbound meta from Conversation Attachments. Returns (normalized, error_notice)."""
    name = str(item.get("name") or item.get("filename") or "attachment")
    mime = str(item.get("mimeType") or item.get("mime_type") or "").strip().lower()
    url = str(item.get("url") or "").strip()
    try:
        size = int(item.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if not url:
        return None, f"{name}（缺少 url）"
    reason = validate_attachment_bytes(filename=name, mime=mime or "application/octet-stream", size=size or 1)
    # size 0 in meta: still try download later; only fail hard mime
    if mime and mime not in allowed_email_mimes():
        return None, f"{name}（MIME 不允许：{mime}）"
    if size > int(settings.email_attachment_max_bytes):
        return None, f"{name}（超过 {settings.email_attachment_max_bytes / (1024 * 1024):g}MB）"
    kind = str(item.get("kind") or "").strip().lower() or kind_from_mime(mime)
    att_id = str(item.get("id") or "").strip() or uuid.uuid4().hex
    return (
        {
            "id": att_id.replace("-", ""),
            "kind": kind if kind in {"image", "video", "file"} else kind_from_mime(mime),
            "name": name,
            "mimeType": mime or "application/octet-stream",
            "size": size,
            "url": url,
        },
        None,
    )


class EmailAttachmentPipeline:
    """Gmail inbound parts → S3 → Portal attachments[]; notices for illegal items."""

    def __init__(self, uploader: S3Uploader | None = None) -> None:
        prefix = settings.s3_file_prefix or "files"
        self.uploader = uploader or S3Uploader(prefix=prefix)
        self.allowed = allowed_email_mimes()
        self.max_bytes = int(settings.email_attachment_max_bytes)
        self.max_count = int(settings.email_attachment_max_count)

    def process_gmail_parts(
        self,
        *,
        gmail: Any,
        message_id: str,
        parts: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        attachments: list[dict[str, Any]] = []
        notices: list[str] = []
        if not parts:
            return attachments, notices

        s3_ok = self.uploader.configured()
        accepted = 0
        for part in parts:
            filename = str(part.get("filename") or "attachment")
            mime = str(part.get("mimeType") or part.get("mime") or "application/octet-stream").lower()
            att_id = str(part.get("attachmentId") or part.get("attachment_id") or "")
            size_hint = int(part.get("size") or 0)

            if accepted >= self.max_count:
                notices.append(f"{filename}（超出数量上限 {self.max_count} 已忽略）")
                continue

            pre = validate_attachment_bytes(
                filename=filename,
                mime=mime,
                size=size_hint or 1,
                allowed=self.allowed,
                max_bytes=self.max_bytes,
            )
            # size_hint 0: still download then re-check
            if pre and size_hint > 0:
                notices.append(pre)
                continue
            if mime not in self.allowed:
                notices.append(f"{filename}（MIME 不允许：{mime}）")
                continue
            if not att_id:
                notices.append(f"{filename}（缺少 attachmentId）")
                continue
            if not s3_ok:
                notices.append(f"{filename}（S3 未配置未上传）")
                continue

            try:
                data = gmail.download_attachment(message_id, att_id)
            except Exception as e:  # noqa: BLE001
                log.exception("gmail attachment download failed mid=%s att=%s", message_id, att_id)
                notices.append(f"{filename}（下载失败：{e}）")
                continue

            size = len(data or b"")
            bad = validate_attachment_bytes(
                filename=filename,
                mime=mime,
                size=size,
                allowed=self.allowed,
                max_bytes=self.max_bytes,
            )
            if bad:
                notices.append(bad)
                continue

            up = self.upload_bytes(data, filename=filename, mime=mime)
            if not up.get("ok"):
                notices.append(f"{filename}（上传失败：{up.get('error') or 'unknown'}）")
                continue
            attachments.append(up["attachment"])
            accepted += 1

        return attachments, notices

    def upload_bytes(self, data: bytes, *, filename: str, mime: str) -> dict[str, Any]:
        att_id = uuid.uuid4().hex
        ext = _ext_for_mime(mime, filename)
        # Override key layout: files/{id}.ext
        safe_name = f"{att_id}{ext}"
        result = self.uploader.upload_bytes_with_key(
            data,
            key=f"{(settings.s3_file_prefix or 'files').strip().strip('/')}/{safe_name}",
            content_type=mime,
            filename=filename or safe_name,
        )
        if not result.get("ok"):
            return result
        attachment = {
            "id": att_id,
            "kind": kind_from_mime(mime),
            "name": Path(filename or safe_name).name,
            "mimeType": mime,
            "size": len(data),
            "url": result["mediaUrl"],
        }
        return {"ok": True, "attachment": attachment, **result}

    def prepare_outbound(
        self, raw_items: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Filter Conversation Attachments for send; return (valid_meta, notices)."""
        valid: list[dict[str, Any]] = []
        notices: list[str] = []
        for item in raw_items:
            meta, err = validate_attachment_meta(item)
            if err or not meta:
                notices.append(err or "未知附件非法")
                continue
            if len(valid) >= self.max_count:
                notices.append(f"{meta['name']}（超出数量上限 {self.max_count} 已忽略）")
                continue
            valid.append(meta)
        return valid, notices

    def download_for_send(self, metas: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """HTTP GET each url → {filename, mime, bytes} for Gmail attach."""
        files: list[dict[str, Any]] = []
        notices: list[str] = []
        for meta in metas:
            url = meta["url"]
            name = meta["name"]
            try:
                with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                    r = client.get(url)
                    if r.status_code >= 400:
                        notices.append(f"{name}（下载失败 HTTP {r.status_code}）")
                        continue
                    data = r.content
            except Exception as e:  # noqa: BLE001
                notices.append(f"{name}（下载失败：{e}）")
                continue
            bad = validate_attachment_bytes(
                filename=name,
                mime=meta.get("mimeType") or "application/octet-stream",
                size=len(data),
                allowed=self.allowed,
                max_bytes=self.max_bytes,
            )
            if bad:
                notices.append(bad)
                continue
            files.append(
                {
                    "filename": name,
                    "mime": meta.get("mimeType") or "application/octet-stream",
                    "bytes": data,
                }
            )
        return files, notices
