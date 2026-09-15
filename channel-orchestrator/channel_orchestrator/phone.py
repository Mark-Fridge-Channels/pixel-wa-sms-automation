from __future__ import annotations

import re


def normalize_e164(raw: str | None, default_region: str = "US") -> str | None:
    """Normalize phone to E.164-ish +digits. US default if 10 digits."""
    e164, _ = normalize_e164_with_reason(raw, default_region=default_region)
    return e164


def normalize_e164_with_reason(
    raw: str | None, default_region: str = "US"
) -> tuple[str | None, str | None]:
    """Return (e164, None) on success, or (None, human-readable Chinese reason) on failure."""
    if raw is None:
        return None, "手机格式识别失败：号码为空"
    s = str(raw).strip()
    if not s:
        return None, "手机格式识别失败：号码为空"

    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None, f"手机格式识别失败：未找到有效数字（原始={s!r}）"

    if has_plus:
        if len(digits) < 8:
            return None, f"手机格式识别失败：国际号位数过短（原始={s!r}）"
        return f"+{digits}", None

    if default_region == "US" and len(digits) == 10:
        return f"+1{digits}", None
    if default_region == "US" and len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}", None
    if len(digits) >= 11:
        return f"+{digits}", None

    return (
        None,
        f"手机格式识别失败：无法归一化为 E.164（默认区={default_region}，位数={len(digits)}，原始={s!r}）",
    )


def thread_id_for_phone(e164: str, channel: str = "SMS") -> str:
    """Deprecated local helper — NOT Portal threadId.

    Portal threadId must come from Task→Conversation.Thread ID and may change
    per task. Do not use this value for POST /api/replies.
    """
    prefix = "wa" if channel.upper() in {"WHATSAPP", "WA"} else "sms"
    return f"{prefix}:{e164}"


def digits_only(e164: str) -> str:
    return re.sub(r"\D", "", e164 or "")
