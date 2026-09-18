"""Guards against double-send when a prior attempt may have already gone out."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

# Notion Task Notes / Companion error marker: do not blindly reset to Pending & retry.
UNCERTAIN_NOTES_PREFIX = "【发送结果未确认】"
UNCERTAIN_ERROR_PREFIX = "UNCERTAIN:"

_HARD_SMS_MARKERS = (
    "502",
    "503",
    "401",
    "403",
    "400",
    "404",
    "bad gateway",
    "sim ",
    "simnumber",
    "unauthorized",
    "forbidden",
)
_UNCERTAIN_SMS_MARKERS = (
    "timed out",
    "timeout",
    "time-out",
    "connection reset",
    "broken pipe",
    "incomplete",
    "eof",
    "temporarily unavailable",
    "remote end closed",
    "connection aborted",
)


def uncertain_notes(detail: str) -> str:
    body = detail.strip()
    if body.startswith(UNCERTAIN_NOTES_PREFIX):
        return body[:1900]
    return f"{UNCERTAIN_NOTES_PREFIX}{body}"[:1900]


def is_uncertain_notes(notes: str | None) -> bool:
    return bool(notes) and (
        notes.startswith(UNCERTAIN_NOTES_PREFIX) or UNCERTAIN_ERROR_PREFIX in notes
    )


def is_uncertain_error(error: str | None) -> bool:
    if not error:
        return False
    if error.startswith(UNCERTAIN_ERROR_PREFIX):
        return True
    if UNCERTAIN_NOTES_PREFIX in error:
        return True
    # Companion legacy wording
    if "未确认" in error or "发送超时" in error:
        return True
    return False


def strip_uncertain_error_prefix(error: str | None) -> str:
    if not error:
        return ""
    if error.startswith(UNCERTAIN_ERROR_PREFIX):
        return error[len(UNCERTAIN_ERROR_PREFIX) :].strip()
    return error.strip()


def classify_sms_send_error(exc: BaseException | str) -> str:
    """Return 'uncertain' | 'hard' for SMS gateway exceptions."""
    msg = str(exc).lower()
    if any(m in msg for m in _UNCERTAIN_SMS_MARKERS):
        return "uncertain"
    if any(m in msg for m in _HARD_SMS_MARKERS):
        return "hard"
    # Default: treat unknown transport errors as uncertain (safer against double-send).
    return "uncertain"


def attempts_path() -> Path:
    return settings.resolved_data_dir() / "outbound_attempts.json"


def _load_attempts() -> dict[str, Any]:
    path = attempts_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_attempts(data: dict[str, Any]) -> None:
    path = attempts_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_attempt(
    task_id: str,
    *,
    channel: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    data = _load_attempts()
    data[task_id] = {
        "channel": channel,
        "outcome": outcome,  # hard_failed | uncertain | completed | skipped
        "detail": (detail or "")[:500],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _save_attempts(data)


def recent_uncertain_attempt(
    task_id: str,
    *,
    cooldown_seconds: int | None = None,
) -> dict[str, Any] | None:
    cooldown = (
        settings.outbound_uncertain_cooldown_seconds
        if cooldown_seconds is None
        else cooldown_seconds
    )
    entry = _load_attempts().get(task_id)
    if not entry or entry.get("outcome") != "uncertain":
        return None
    try:
        at = datetime.fromisoformat(str(entry["at"]).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return entry
    age = (datetime.now(timezone.utc) - at.astimezone(timezone.utc)).total_seconds()
    if age <= cooldown:
        return entry
    return None


def conversation_already_sent(notion: Any, conversation_id: str | None) -> bool:
    """True when outbound Conversation already has Interaction At (schema dropped Message Status)."""
    if not conversation_id:
        return False
    try:
        page = notion.get_page(conversation_id)
        from .notion_props import date_start, props, rich_text_plain

        p = props(page)
        if date_start(p.get("Interaction At")):
            return True
        notes = rich_text_plain(p.get("Notes")) or ""
        return notes.startswith("已发送")
    except Exception:  # noqa: BLE001
        log.exception("failed to read conversation send markers")
        return False


def task_notes(notion: Any, task_id: str) -> str:
    try:
        page = notion.get_page(task_id)
        from .notion_props import props, rich_text_plain

        return rich_text_plain(props(page).get("Notes")) or ""
    except Exception:  # noqa: BLE001
        return ""


_FORCE_OK_RE = re.compile(r"【允许重试】")


def retry_blocked_reason(
    *,
    notion: Any,
    task_id: str,
    conversation_id: str | None,
    force: bool = False,
) -> str | None:
    """If non-None, execute_plan_item should skip sending."""
    if force:
        return None
    notes = task_notes(notion, task_id)
    if _FORCE_OK_RE.search(notes or ""):
        return None
    if is_uncertain_notes(notes):
        return (
            f"任务 Notes 含{UNCERTAIN_NOTES_PREFIX}：禁止直接重试，"
            "请先确认渠道侧是否已发出；确认后可在 Notes 加【允许重试】或使用 --force"
        )
    recent = recent_uncertain_attempt(task_id)
    if recent:
        return (
            f"该任务在冷却期内曾「发送结果未确认」（{recent.get('at')}）："
            "禁止自动重试，避免双发；确认后加【允许重试】或 --force"
        )
    if conversation_already_sent(notion, conversation_id):
        return "关联 Conversation 已发送（Interaction At 已填）：跳过重发，避免双发"
    return None
