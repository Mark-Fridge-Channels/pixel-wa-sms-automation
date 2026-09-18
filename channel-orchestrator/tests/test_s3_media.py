"""Unit tests for WA media helpers (S3 URL shape + type normalize)."""

from __future__ import annotations

from channel_orchestrator.s3_media import (
    S3Uploader,
    content_placeholder,
    filename_from_url,
    media_type_normalize,
    mime_for_media_type,
)


def test_media_type_normalize_aliases():
    assert media_type_normalize("IMG") == "image"
    assert media_type_normalize("photo") == "image"
    assert media_type_normalize("voice") == "audio"
    assert media_type_normalize("document") == "file"
    assert media_type_normalize("video") == "video"
    assert media_type_normalize("weird") is None


def test_content_placeholder():
    assert content_placeholder("image") == "[image]"
    assert content_placeholder("audio") == "[audio]"


def test_mime_for_media_type_from_filename():
    assert mime_for_media_type("image", "pic.png") == "image/png"
    assert mime_for_media_type("video", None) == "video/mp4"


def test_filename_from_url():
    assert filename_from_url("https://cdn.example.com/a/b/hello%20x.mp4") == "hello%20x.mp4"
    assert filename_from_url("https://cdn.example.com/") == "media.bin"


def test_s3_uploader_not_configured():
    up = S3Uploader(bucket="", access_key="x", secret_key="y")
    assert not up.configured()
    result = up.upload_bytes(b"abc", filename="a.jpg", media_type="image")
    assert result["ok"] is False
    assert "S3" in result["error"]


def test_s3_uploader_public_url_shape(monkeypatch):
    calls: dict = {}

    class FakeClient:
        def put_object(self, **kwargs):
            calls.update(kwargs)

    def fake_client(*args, **kwargs):
        return FakeClient()

    import sys
    import types

    boto3 = types.ModuleType("boto3")
    boto3.client = fake_client  # type: ignore[attr-defined]
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")

    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        pass

    exceptions.BotoCoreError = BotoCoreError  # type: ignore[attr-defined]
    exceptions.ClientError = ClientError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)

    up = S3Uploader(
        bucket="my-bucket",
        region="us-west-2",
        prefix="wa-inbound",
        public_base_url="https://cdn.example.com",
        access_key="AKIA",
        secret_key="secret",
    )
    result = up.upload_bytes(b"hello", filename="shot.jpg", content_type="image/jpeg", media_type="image")
    assert result["ok"] is True
    assert result["mediaUrl"].startswith("https://cdn.example.com/wa-inbound/image/")
    assert result["mediaUrl"].endswith("_shot.jpg")
    assert result["contentType"] == "image/jpeg"
    assert calls["Bucket"] == "my-bucket"
    assert calls["Body"] == b"hello"
