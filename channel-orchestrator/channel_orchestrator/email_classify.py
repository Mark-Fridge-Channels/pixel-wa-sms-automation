from __future__ import annotations

import re
from typing import Any


# Hard bounce: address invalid / permanent failure
_HARD_SUBJECT = (
    r"undeliverable",
    r"delivery\s*status\s*notification",
    r"returned\s*mail",
    r"mail\s*delivery\s*failed",
    r"failure\s*notice",
    r"delivery\s*failure",
    r"非送达",
    r"无法投递",
    r"投递失败",
)
_HARD_BODY = (
    r"user\s*unknown",
    r"unknown\s*user",
    r"does\s*not\s*exist",
    r"recipient\s*address\s*rejected",
    r"mailbox\s*not\s*found",
    r"no\s*such\s*user",
    r"invalid\s*recipient",
    r"550\s*5\.1\.1",
    r"550\s*5\.1\.10",
    r"550\s*5\.4\.1",
    r"551\s*5\.1\.1",
    r"553\s*5\.1\.2",
    r"address\s*rejected",
    r"not\s*a\s*valid",
    r"unrouteable",
)

# Soft bounce: temporary
_SOFT_BODY = (
    r"mailbox\s*full",
    r"over\s*quota",
    r"quota\s*exceeded",
    r"try\s*again\s*later",
    r"temporarily\s*(deferred|unavailable|rejected)",
    r"greylisted",
    r"452\s*4\.",
    r"421\s*4\.",
    r"450\s*4\.",
    r"out\s*of\s*storage",
)

_AUTO_SUBJECT = (
    r"out\s*of\s*office",
    r"automatic\s*reply",
    r"auto[\-\s]?reply",
    r"away\s*from\s*(the\s*)?office",
    r"休假自动回复",
)


def classify_inbound_email(
    *,
    sender: str | None,
    subject: str | None,
    body: str | None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Classify system vs human inbound email.

    kind:
      human | hard_bounce | soft_bounce | auto_reply | system_other
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    from_l = (sender or headers.get("from") or "").lower()
    subj = subject or ""
    subj_l = subj.lower()
    body_l = (body or "").lower()
    blob = f"{subj_l}\n{body_l}\n{from_l}"

    auto = (headers.get("auto-submitted") or "").lower()
    if auto and auto != "no":
        # Delivery Status Notification often has auto-submitted
        if _match_any(blob, _HARD_SUBJECT) or _match_any(blob, _HARD_BODY):
            return _result("hard_bounce", "Email硬退信：地址无效或永久拒收")
        if _match_any(blob, _SOFT_BODY):
            return _result("soft_bounce", "Email软退信：暂时无法投递")
        if _match_any(subj_l, _AUTO_SUBJECT) or "vacation" in blob:
            return _result("auto_reply", "Email自动回复（Out of Office）")
        return _result("system_other", "Email系统回执（非人工），已忽略")

    if "mailer-daemon@" in from_l or "postmaster@" in from_l:
        if _match_any(blob, _SOFT_BODY) and not _match_any(blob, _HARD_BODY):
            return _result("soft_bounce", "Email软退信：暂时无法投递（Mailer-Daemon）")
        return _result("hard_bounce", "Email硬退信：Mailer-Daemon 退信")

    if _match_any(subj_l, _HARD_SUBJECT) or _match_any(blob, _HARD_BODY):
        return _result("hard_bounce", "Email硬退信：地址无效或永久拒收")

    if _match_any(blob, _SOFT_BODY):
        return _result("soft_bounce", "Email软退信：暂时无法投递")

    if _match_any(subj_l, _AUTO_SUBJECT):
        return _result("auto_reply", "Email自动回复（Out of Office）")

    if headers.get("x-auto-response-suppress") and not body_l.strip():
        return _result("system_other", "Email系统回执（空正文），已忽略")

    return _result("human", "")


def _match_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def _result(kind: str, reason: str) -> dict[str, Any]:
    return {"kind": kind, "reason": reason, "is_human": kind == "human"}
