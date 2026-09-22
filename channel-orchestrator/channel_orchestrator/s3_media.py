from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def media_type_normalize(raw: str | None) -> str | None:
    if not raw:
        return None
    t = str(raw).strip().lower()
    aliases = {
        "image": "image",
        "img": "image",
        "photo": "image",
        "picture": "image",
        "video": "video",
        "audio": "audio",
        "voice": "audio",
        "file": "file",
        "document": "file",
        "doc": "file",
    }
    return aliases.get(t, t if t in {"image", "video", "audio", "file"} else None)


def mime_for_media_type(media_type: str, filename: str | None = None) -> str:
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return {
        "image": "image/jpeg",
        "video": "video/mp4",
        "audio": "audio/mpeg",
        "file": "application/octet-stream",
    }.get(media_type, "application/octet-stream")


def content_placeholder(media_type: str) -> str:
    return {
        "image": "[image]",
        "video": "[video]",
        "audio": "[audio]",
        "file": "[file]",
    }.get(media_type, "[media]")


class S3Uploader:
    """Upload inbound/outbound media bytes to S3; returns HTTPS object URL."""

    def __init__(
        self,
        *,
        bucket: str | None = None,
        region: str | None = None,
        prefix: str | None = None,
        public_base_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        from .config import settings

        self.bucket = (bucket if bucket is not None else settings.s3_bucket).strip()
        self.region = (region if region is not None else settings.aws_region).strip() or "us-east-1"
        self.prefix = (prefix if prefix is not None else settings.s3_prefix).strip().strip("/")
        self.public_base_url = (
            public_base_url if public_base_url is not None else settings.s3_public_base_url
        ).rstrip("/")
        self.access_key = access_key if access_key is not None else settings.aws_access_key_id
        self.secret_key = secret_key if secret_key is not None else settings.aws_secret_access_key

    def configured(self) -> bool:
        return bool(self.bucket and self.access_key and self.secret_key)

    def upload_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str | None = None,
        media_type: str | None = None,
    ) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "error": "S3 not configured (S3_BUCKET / AWS keys)"}
        if not data:
            return {"ok": False, "error": "empty media bytes"}

        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError:
            return {"ok": False, "error": "boto3 not installed"}

        safe_name = Path(filename or "media.bin").name.replace(" ", "_")
        key_parts = [p for p in [self.prefix, media_type or "file", f"{uuid.uuid4().hex}_{safe_name}"] if p]
        key = "/".join(key_parts)
        ctype = content_type or mime_for_media_type(media_type or "file", safe_name)

        try:
            client = boto3.client(
                "s3",
                region_name=self.region,
                aws_access_key_id=self.access_key or None,
                aws_secret_access_key=self.secret_key or None,
            )
            extra = {"ContentType": ctype}
            # Optional public-read if bucket policy expects ACL; default omit ACL.
            client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        except (BotoCoreError, ClientError, Exception) as e:  # noqa: BLE001
            log.exception("S3 upload failed")
            return {"ok": False, "error": str(e)}

        if self.public_base_url:
            url = f"{self.public_base_url}/{key}"
        else:
            url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{key}"

        return {
            "ok": True,
            "mediaUrl": url,
            "s3Bucket": self.bucket,
            "s3Key": key,
            "contentType": ctype,
            "mediaType": media_type,
            "filename": safe_name,
            "size": len(data),
        }

    def upload_bytes_with_key(
        self,
        data: bytes,
        *,
        key: str,
        content_type: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Upload with an exact object key (e.g. files/{id}.pdf for Email attachments)."""
        if not self.configured():
            return {"ok": False, "error": "S3 not configured (S3_BUCKET / AWS keys)"}
        if not data:
            return {"ok": False, "error": "empty media bytes"}
        if not key or not str(key).strip():
            return {"ok": False, "error": "empty s3 key"}

        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError:
            return {"ok": False, "error": "boto3 not installed"}

        safe_key = str(key).lstrip("/")
        ctype = content_type or mime_for_media_type("file", filename)
        try:
            client = boto3.client(
                "s3",
                region_name=self.region,
                aws_access_key_id=self.access_key or None,
                aws_secret_access_key=self.secret_key or None,
            )
            client.put_object(Bucket=self.bucket, Key=safe_key, Body=data, ContentType=ctype)
        except (BotoCoreError, ClientError, Exception) as e:  # noqa: BLE001
            log.exception("S3 upload failed key=%s", safe_key)
            return {"ok": False, "error": str(e)}

        if self.public_base_url:
            url = f"{self.public_base_url}/{safe_key}"
        else:
            url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{safe_key}"

        return {
            "ok": True,
            "mediaUrl": url,
            "s3Bucket": self.bucket,
            "s3Key": safe_key,
            "contentType": ctype,
            "filename": filename or Path(safe_key).name,
            "size": len(data),
        }


def filename_from_url(url: str, fallback: str = "media.bin") -> str:
    path = urlparse(url).path
    name = Path(path).name
    return name or fallback


def is_allowed_s3_media_url(url: str) -> bool:
    """True when url is this bucket's public HTTPS object (or optional CDN base)."""
    from .config import settings

    text = (url or "").strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    bucket = (settings.s3_bucket or "").strip()
    region = (settings.aws_region or "").strip() or "us-east-1"
    allowed_hosts: set[str] = set()
    if bucket:
        allowed_hosts.add(f"{bucket}.s3.{region}.amazonaws.com".lower())
        allowed_hosts.add(f"{bucket}.s3.amazonaws.com".lower())
    base = (settings.s3_public_base_url or "").strip()
    if base:
        try:
            base_host = (urlparse(base).hostname or "").lower()
        except ValueError:
            base_host = ""
        if base_host:
            allowed_hosts.add(base_host)
    if host in allowed_hosts:
        return bool(parsed.path and parsed.path != "/")
    if bucket and host in {f"s3.{region}.amazonaws.com".lower(), "s3.amazonaws.com"}:
        return parsed.path == f"/{bucket}" or parsed.path.startswith(f"/{bucket}/")
    return False

