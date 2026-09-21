from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .cache import OutboundCache
from .channels import normalize_channel
from .config import settings
from .gateway import SmsGatewayClient
from .heartbeat import touch_heartbeat
from .notion_client import NotionClient, ResolvedTask
from .scheduler import DailyPlan, PlanItem, due_items, load_plan, save_plan
from .send_safety import (
    classify_sms_send_error,
    is_uncertain_error,
    record_attempt,
    retry_blocked_reason,
    strip_uncertain_error_prefix,
    uncertain_notes,
)
from .wa_jobs import get_wa_queue

log = logging.getLogger(__name__)


def execute_resolved_sms(
    resolved: ResolvedTask,
    *,
    notion: NotionClient | None = None,
    gateway: SmsGatewayClient | None = None,
    cache: OutboundCache | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    notion = notion or NotionClient()
    gateway = gateway or SmsGatewayClient()
    cache = cache or OutboundCache(channel="SMS")
    now = datetime.now(timezone.utc)

    if resolved.resolve_error:
        if not dry_run:
            notion.update_task_status(
                resolved.task_id, "Failed", ended_at=now, notes=resolved.resolve_error
            )
            if resolved.conversation_id:
                notion.update_conversation_outbound(
                    resolved.conversation_id,
                    message_status="Failed",
                    interaction_at=now,
                    sender=settings.saily_phone_e164 or None,
                    message_id=None,
                    thread_id=resolved.thread_id,
                )
            if resolved.phone_e164:
                cache.mark_failed(resolved.phone_e164)
        return {"ok": False, "status": "failed", "reason": resolved.resolve_error, "task_id": resolved.task_id}

    assert resolved.phone_e164 and resolved.content and resolved.conversation_id and resolved.thread_id

    if dry_run:
        return {
            "ok": True,
            "status": "dry_run",
            "task_id": resolved.task_id,
            "phone": resolved.phone_e164,
            "thread_id": resolved.thread_id,
            "content": resolved.content,
            "channel": "SMS",
        }

    # Cache before send so a fast reply can still resolve task/thread pair after success.
    cache.set_pending(
        resolved.phone_e164,
        task_page_id=resolved.task_id,
        thread_id=resolved.thread_id,
        contact_page_id=resolved.contact_id,
        conversation_page_id=resolved.conversation_id,
    )

    notion.update_task_status(resolved.task_id, "In Progress")
    try:
        resp = gateway.send_sms(
            [resolved.phone_e164],
            resolved.content,
            sim_number=settings.sms_sim_number,
        )
        touch_heartbeat("phone")
    except Exception as e:  # noqa: BLE001
        kind = classify_sms_send_error(e)
        msg = f"短信发送失败：{e}"
        log.exception(msg)
        if kind == "uncertain":
            notes = uncertain_notes(
                "请求可能已被 Gateway 接受但本地未确认成功。"
                "请勿直接改回 Pending 重试；先查 Gateway / 手机发件箱。"
                f" 原因：{e}"
            )
            # Stay In Progress — scheduler only picks Pending, so no auto re-send.
            notion.update_task_status(resolved.task_id, "In Progress", notes=notes)
            record_attempt(resolved.task_id, channel="SMS", outcome="uncertain", detail=str(e))
            return {
                "ok": False,
                "status": "uncertain",
                "reason": notes,
                "task_id": resolved.task_id,
            }
        notion.update_task_status(resolved.task_id, "Failed", ended_at=now, notes=msg)
        notion.update_conversation_outbound(
            resolved.conversation_id,
            message_status="Failed",
            interaction_at=now,
            sender=settings.saily_phone_e164 or None,
            message_id=None,
            thread_id=resolved.thread_id,
        )
        cache.mark_failed(resolved.phone_e164)
        record_attempt(resolved.task_id, channel="SMS", outcome="hard_failed", detail=str(e))
        return {"ok": False, "status": "failed", "reason": msg, "task_id": resolved.task_id}

    message_id = None
    if isinstance(resp, dict):
        message_id = str(resp.get("id") or resp.get("messageId") or "") or None

    if not message_id:
        notes = uncertain_notes(
            "Gateway 已响应但未返回 message id，无法确认是否入队。"
            "请勿直接改回 Pending 重试；先查 Gateway。"
        )
        notion.update_task_status(resolved.task_id, "In Progress", notes=notes)
        record_attempt(
            resolved.task_id, channel="SMS", outcome="uncertain", detail="missing gateway id"
        )
        return {
            "ok": False,
            "status": "uncertain",
            "reason": notes,
            "task_id": resolved.task_id,
            "gateway": resp,
        }

    sent_at = datetime.now(timezone.utc)
    notion.update_task_status(resolved.task_id, "Completed", ended_at=sent_at)
    notion.update_conversation_outbound(
        resolved.conversation_id,
        message_status="Sent",
        interaction_at=sent_at,
        sender=settings.saily_phone_e164 or None,
        message_id=message_id,
        thread_id=resolved.thread_id,
    )
    cache.set_ready(
        resolved.phone_e164,
        task_page_id=resolved.task_id,
        thread_id=resolved.thread_id,
        contact_page_id=resolved.contact_id,
        conversation_page_id=resolved.conversation_id,
        sent_at=sent_at,
    )
    record_attempt(resolved.task_id, channel="SMS", outcome="completed", detail=message_id)
    return {
        "ok": True,
        "status": "completed",
        "task_id": resolved.task_id,
        "phone": resolved.phone_e164,
        "thread_id": resolved.thread_id,
        "gateway": resp,
        "message_id": message_id,
        "channel": "SMS",
    }


def enqueue_whatsapp_job(
    resolved: ResolvedTask,
    *,
    notion: NotionClient | None = None,
    cache: OutboundCache | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Resolve OK → In Progress + queue job for Companion; Notion Completed on job result.

    Mark outbound cache ready at enqueue time (not only after Companion confirms send)
    so WhatsApp replies during send / after uncertain timeout still match Portal ingest.
    """
    notion = notion or NotionClient()
    cache = cache or OutboundCache(channel="WHATSAPP")
    now = datetime.now(timezone.utc)
    queue = get_wa_queue()

    if resolved.resolve_error:
        if not dry_run:
            notion.update_task_status(
                resolved.task_id, "Failed", ended_at=now, notes=resolved.resolve_error
            )
            if resolved.conversation_id:
                notion.update_conversation_outbound(
                    resolved.conversation_id,
                    message_status="Failed",
                    interaction_at=now,
                    sender=settings.saily_phone_e164 or None,
                    message_id=None,
                    thread_id=resolved.thread_id,
                )
            if resolved.phone_e164:
                cache.mark_failed(resolved.phone_e164)
        return {"ok": False, "status": "failed", "reason": resolved.resolve_error, "task_id": resolved.task_id}

    assert resolved.phone_e164 and resolved.conversation_id and resolved.thread_id
    media_url = str((resolved.extended_parameters or {}).get("mediaUrl") or "").strip()
    media_type = str((resolved.extended_parameters or {}).get("mediaType") or "").strip().lower()
    if not resolved.content and not media_url:
        raise AssertionError("WA job requires content or mediaUrl")

    if dry_run:
        return {
            "ok": True,
            "status": "dry_run",
            "task_id": resolved.task_id,
            "phone": resolved.phone_e164,
            "thread_id": resolved.thread_id,
            "content": resolved.content,
            "media_url": media_url or None,
            "media_type": media_type or None,
            "channel": "WHATSAPP",
        }

    day_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # 0 = disabled (no daily WhatsApp cap)
    if settings.wa_daily_send_limit > 0 and queue.count_done_today(day_utc) >= settings.wa_daily_send_limit:
        msg = f"已达当日 WhatsApp 发送上限（{settings.wa_daily_send_limit}）"
        notion.update_task_status(resolved.task_id, "Failed", ended_at=now, notes=msg)
        return {"ok": False, "status": "failed", "reason": msg, "task_id": resolved.task_id}

    # Ready at enqueue: WA is a continuous chat; replies may arrive before Companion ACKs.
    cache.set_ready(
        resolved.phone_e164,
        task_page_id=resolved.task_id,
        thread_id=resolved.thread_id,
        contact_page_id=resolved.contact_id,
        conversation_page_id=resolved.conversation_id,
        sent_at=now,
    )
    notion.update_task_status(resolved.task_id, "In Progress")
    job_body: dict[str, Any] = {
        "task_id": resolved.task_id,
        "contact_id": resolved.contact_id,
        "conversation_id": resolved.conversation_id,
        "thread_id": resolved.thread_id,
        "phone": resolved.phone_e164,
        "text": resolved.content or "",
        "channel": "WHATSAPP",
    }
    if media_url and media_type in {"image", "video"}:
        job_body["media_url"] = media_url
        job_body["media_type"] = media_type
    job = queue.enqueue(job_body)
    return {
        "ok": True,
        "status": "queued",
        "task_id": resolved.task_id,
        "job_id": job["id"],
        "phone": resolved.phone_e164,
        "thread_id": resolved.thread_id,
        "media_url": media_url or None,
        "media_type": media_type or None,
        "channel": "WHATSAPP",
    }


def finalize_whatsapp_job(
    job: dict[str, Any],
    *,
    ok: bool,
    error: str | None = None,
    notion: NotionClient | None = None,
    cache: OutboundCache | None = None,
) -> dict[str, Any]:
    if str(job.get("job_type") or "").lower() == "probe":
        return {"ok": True, "status": "probe", "skipped_notion": True}

    notion = notion or NotionClient()
    cache = cache or OutboundCache(channel="WHATSAPP")
    now = datetime.now(timezone.utc)
    task_id = job.get("task_id")
    conversation_id = job.get("conversation_id")
    phone = job.get("phone")
    contact_id = job.get("contact_id")
    thread_id = job.get("thread_id")

    if not task_id:
        return {"ok": False, "error": "job missing task_id"}

    if ok:
        notion.update_task_status(task_id, "Completed", ended_at=now)
        if conversation_id:
            notion.update_conversation_outbound(
                conversation_id,
                message_status="Sent",
                interaction_at=now,
                sender=settings.saily_phone_e164 or None,
                message_id=job.get("id"),
                thread_id=thread_id,
            )
        if phone:
            cache.set_ready(
                phone,
                task_page_id=task_id,
                thread_id=thread_id,
                contact_page_id=contact_id,
                conversation_page_id=conversation_id,
                sent_at=now,
            )
        touch_heartbeat("phone")
        record_attempt(task_id, channel="WHATSAPP", outcome="completed", detail=job.get("id"))
        return {"ok": True, "status": "completed", "task_id": task_id}

    raw_err = error or "WhatsApp 发送失败"
    if is_uncertain_error(raw_err):
        detail = strip_uncertain_error_prefix(raw_err)
        notes = uncertain_notes(
            f"{detail}。可能已发出但未确认；请勿直接改回 Pending 重试。"
            "确认未发出后可在 Notes 加【允许重试】或使用 --force。"
        )
        notion.update_task_status(task_id, "In Progress", notes=notes)
        record_attempt(task_id, channel="WHATSAPP", outcome="uncertain", detail=detail)
        return {"ok": False, "status": "uncertain", "reason": notes, "task_id": task_id}

    msg = raw_err
    notion.update_task_status(task_id, "Failed", ended_at=now, notes=msg)
    if conversation_id:
        notion.update_conversation_outbound(
            conversation_id,
            message_status="Failed",
            interaction_at=now,
            sender=settings.saily_phone_e164 or None,
            message_id=None,
            thread_id=thread_id,
        )
    if phone:
        cache.mark_failed(phone)
    record_attempt(task_id, channel="WHATSAPP", outcome="hard_failed", detail=msg)
    return {"ok": False, "status": "failed", "task_id": task_id, "reason": msg}


def fail_email_task_from_bounce(
    *,
    cache_entry: dict[str, Any],
    reason: str,
    kind: str,
    notion: NotionClient | None = None,
    cache: OutboundCache | None = None,
) -> dict[str, Any]:
    """After outbound Completed, hard/soft bounce → Task Failed with distinct Notes."""
    notion = notion or NotionClient()
    cache = cache or OutboundCache(channel="EMAIL")
    now = datetime.now(timezone.utc)
    task_id = cache_entry.get("task_page_id")
    conversation_id = cache_entry.get("conversation_page_id")
    email = cache_entry.get("email")
    thread_id = cache_entry.get("thread_id")
    if not task_id:
        return {"ok": False, "reason": "no_task"}

    notes = reason or f"Email投递失败（{kind}）"
    notion.update_task_status(task_id, "Failed", ended_at=now, notes=notes)
    if conversation_id:
        notion.update_conversation_outbound(
            conversation_id,
            message_status="Failed",
            interaction_at=now,
            sender=settings.gmail_user or None,
            message_id=None,
            thread_id=thread_id,
        )
    if email:
        cache.mark_failed(email)
    return {
        "ok": True,
        "status": "failed",
        "task_id": task_id,
        "kind": kind,
        "reason": notes,
    }


def execute_resolved(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return execute_resolved_sms(*args, **kwargs)


def execute_resolved_email(
    resolved: ResolvedTask,
    *,
    notion: NotionClient | None = None,
    gmail: Any | None = None,
    cache: OutboundCache | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    from .gmail_client import GmailClient

    notion = notion or NotionClient()
    gmail = gmail or GmailClient()
    cache = cache or OutboundCache(channel="EMAIL")
    now = datetime.now(timezone.utc)
    our = settings.gmail_user or None

    if resolved.resolve_error:
        if not dry_run:
            notion.update_task_status(
                resolved.task_id, "Failed", ended_at=now, notes=resolved.resolve_error
            )
            if resolved.conversation_id:
                notion.update_conversation_outbound(
                    resolved.conversation_id,
                    message_status="Failed",
                    interaction_at=now,
                    sender=our,
                    message_id=None,
                    thread_id=resolved.thread_id,
                )
            if resolved.email:
                cache.mark_failed(resolved.email)
        return {
            "ok": False,
            "status": "failed",
            "reason": resolved.resolve_error,
            "task_id": resolved.task_id,
            "channel": "EMAIL",
        }

    assert resolved.email and resolved.content and resolved.conversation_id and resolved.thread_id

    gmail_thread_id = (
        (resolved.extended_parameters or {}).get("gmailThreadId")
        or (resolved.extended_parameters or {}).get("gmail_thread_id")
    )
    subject = resolved.subject or resolved.title or "(no subject)"

    if dry_run:
        return {
            "ok": True,
            "status": "dry_run",
            "task_id": resolved.task_id,
            "email": resolved.email,
            "thread_id": resolved.thread_id,
            "gmail_thread_id": gmail_thread_id,
            "subject": subject,
            "content": resolved.content,
            "channel": "EMAIL",
        }

    cache.set_pending(
        resolved.email,
        task_page_id=resolved.task_id,
        thread_id=resolved.thread_id,
        contact_page_id=resolved.contact_id,
        conversation_page_id=resolved.conversation_id,
        extra={"gmail_thread_id": gmail_thread_id} if gmail_thread_id else None,
    )
    notion.update_task_status(resolved.task_id, "In Progress")
    try:
        sent = gmail.send_new_or_reply(
            to=resolved.email,
            subject=subject,
            body=resolved.content,
            gmail_thread_id=gmail_thread_id,
        )
    except Exception as e:  # noqa: BLE001
        msg = f"Email 发送失败：{e}"
        log.exception(msg)
        notion.update_task_status(resolved.task_id, "Failed", ended_at=now, notes=msg)
        notion.update_conversation_outbound(
            resolved.conversation_id,
            message_status="Failed",
            interaction_at=now,
            sender=our,
            message_id=None,
            thread_id=resolved.thread_id,
        )
        cache.mark_failed(resolved.email)
        return {"ok": False, "status": "failed", "reason": msg, "task_id": resolved.task_id}

    sent_at = datetime.now(timezone.utc)
    gtid = sent.get("gmail_thread_id")
    gmid = sent.get("gmail_message_id")
    notion.update_task_status(resolved.task_id, "Completed", ended_at=sent_at)
    notion.update_conversation_outbound(
        resolved.conversation_id,
        message_status="Sent",
        interaction_at=sent_at,
        sender=our,
        message_id=gmid,
        thread_id=resolved.thread_id,
    )
    ext = {"gmailThreadId": gtid, "gmailMessageId": gmid}
    ext = {k: v for k, v in ext.items() if v}
    if ext:
        try:
            notion.update_conversation_extended_parameters(resolved.conversation_id, ext)
        except Exception:  # noqa: BLE001
            log.exception("failed to write Extended Parameters for %s", resolved.conversation_id)

    cache.set_ready(
        resolved.email,
        task_page_id=resolved.task_id,
        thread_id=resolved.thread_id,
        contact_page_id=resolved.contact_id,
        conversation_page_id=resolved.conversation_id,
        sent_at=sent_at,
        extra={
            "gmail_thread_id": gtid,
            "gmail_message_id": gmid,
            "gmailThreadId": gtid,
            "gmailMessageId": gmid,
        },
    )
    return {
        "ok": True,
        "status": "completed",
        "task_id": resolved.task_id,
        "email": resolved.email,
        "thread_id": resolved.thread_id,
        "gmail_thread_id": gtid,
        "gmail_message_id": gmid,
        "channel": "EMAIL",
    }


def _apply_result_status(item: PlanItem, result: dict[str, Any], *, dry_run: bool) -> None:
    status = result.get("status")
    if status == "skipped":
        item.status = "skipped"
    elif status == "queued":
        item.status = "queued"
        item.job_id = result.get("job_id")
    elif status == "uncertain":
        item.status = "failed"
        item.notes = result.get("reason")
    elif result.get("ok") and status == "dry_run":
        item.status = "planned"
    elif result.get("ok"):
        item.status = "completed" if not dry_run else "planned"
    else:
        item.status = "failed"
        item.notes = result.get("reason")
    if dry_run and status == "dry_run":
        item.status = "planned"


def execute_task(
    task_id: str,
    *,
    channel: str | None = None,
    notion: NotionClient | None = None,
    gateway: SmsGatewayClient | None = None,
    cache: OutboundCache | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Execute one Notion task without daily_plan JSON (scan/queue path)."""
    notion = notion or NotionClient()
    status = notion.get_task_status(task_id)
    if status != "Pending":
        msg = f"执行前复核非 Pending（当前={status}）"
        record_attempt(task_id, channel=normalize_channel(channel or "SMS"), outcome="skipped", detail=msg)
        return {"ok": True, "status": "skipped", "reason": msg, "task_id": task_id, "notion_status": status}

    resolved = notion.resolve_task_for_send(task_id)
    ch = normalize_channel(channel or resolved.channel or "SMS")

    blocked = retry_blocked_reason(
        notion=notion,
        task_id=task_id,
        conversation_id=resolved.conversation_id,
        force=force,
    )
    if blocked:
        record_attempt(task_id, channel=ch, outcome="skipped", detail=blocked)
        return {"ok": True, "status": "skipped", "reason": blocked, "task_id": task_id}

    if ch == "WHATSAPP":
        wa_cache = cache if cache and cache.channel in {"WHATSAPP", "WA"} else OutboundCache(channel="WHATSAPP")
        return enqueue_whatsapp_job(resolved, notion=notion, cache=wa_cache, dry_run=dry_run)

    if ch == "EMAIL":
        email_cache = cache if cache and cache.channel == "EMAIL" else OutboundCache(channel="EMAIL")
        return execute_resolved_email(resolved, notion=notion, cache=email_cache, dry_run=dry_run)

    sms_cache = cache if cache and cache.channel == "SMS" else OutboundCache(channel="SMS")
    return execute_resolved_sms(
        resolved, notion=notion, gateway=gateway, cache=sms_cache, dry_run=dry_run
    )


def execute_plan_item(
    item: PlanItem,
    plan: DailyPlan,
    *,
    notion: NotionClient | None = None,
    gateway: SmsGatewayClient | None = None,
    cache: OutboundCache | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    notion = notion or NotionClient()
    channel = normalize_channel(item.channel or plan.channel or "SMS")
    result = execute_task(
        item.task_id,
        channel=channel,
        notion=notion,
        gateway=gateway,
        cache=cache,
        dry_run=dry_run,
        force=force,
    )
    # Keep plan item fields in sync for legacy daily_plan tools.
    if result.get("status") != "skipped":
        resolved = notion.resolve_task_for_send(item.task_id)
        item.phone = resolved.phone_e164 or resolved.email
        item.conversation_id = resolved.conversation_id
        item.contact_id = resolved.contact_id
    _apply_result_status(item, result, dry_run=dry_run)
    if result.get("status") == "skipped":
        item.notes = result.get("reason")
    save_plan(plan)
    return result


def process_due(
    *,
    day: str | None = None,
    channel: str = "SMS",
    notion: NotionClient | None = None,
    gateway: SmsGatewayClient | None = None,
    cache: OutboundCache | None = None,
    dry_run: bool = False,
    force: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    from .scheduler import ny_today

    day = day or ny_today().isoformat()
    ch = normalize_channel(channel)
    plan = load_plan(day, ch)
    if not plan:
        return [{"ok": False, "error": f"no plan for {day} channel={ch}"}]
    results = []
    for item in due_items(plan, now=now):
        results.append(
            execute_plan_item(
                item,
                plan,
                notion=notion,
                gateway=gateway,
                cache=cache,
                dry_run=dry_run,
                force=force,
            )
        )
    return results
