from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from .channels import normalize_channel
from .config import settings
from .exec_log import append_exec_event, touch_scan_state
from .notion_client import NotionClient
from .notion_props import date_start, props, rich_text_plain, select_name
from .outbound import execute_task
from .outbound_queue import OutboundQueue, get_outbound_queue
from .runtime_settings import get_scan_interval_seconds, get_send_gap_seconds

log = logging.getLogger(__name__)

_last_phone_start_mono = 0.0


def scan_and_enqueue(
    *,
    channels: list[str] | None = None,
    notion: NotionClient | None = None,
    queue: OutboundQueue | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    notion = notion or NotionClient()
    queue = queue or get_outbound_queue()
    now = now or datetime.now(timezone.utc)
    chans = [normalize_channel(c) for c in (channels or ["SMS", "WHATSAPP", "EMAIL"])]
    pages = notion.query_due_pending(channels=chans, now=now)
    active = queue.active_task_ids()
    enqueued: list[dict[str, Any]] = []
    for page in pages:
        task_id = page.get("id")
        if not task_id or task_id in active:
            continue
        p = props(page)
        channel = normalize_channel(select_name(p.get("Channel")) or "SMS")
        if channel not in chans:
            continue
        title = rich_text_plain(p.get("Follow-up Task")) or task_id
        priority = select_name(p.get("Priority")) or "P2"
        scheduled = date_start(p.get("Scheduled At"))
        row = queue.enqueue(
            task_id=task_id,
            channel=channel,
            title=title,
            priority=priority,
            scheduled_at=scheduled,
        )
        if not row:
            continue
        event = append_exec_event(
            {
                "event": "enqueued",
                "task_id": task_id,
                "channel": channel,
                "title": title,
                "priority": priority,
                "scheduled_at": scheduled,
                "queue_id": row["id"],
            }
        )
        enqueued.append(event)
    touch_scan_state(
        last_scan_at=datetime.now(timezone.utc).isoformat(),
        last_scan_found=len(pages),
        last_scan_enqueued=len(enqueued),
        channels=chans,
    )
    log.info("scan done found=%d enqueued=%d", len(pages), len(enqueued))
    return enqueued


def _map_finish_status(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    if status in {"skipped"}:
        return "skipped"
    if status in {"failed", "uncertain"}:
        return status
    if status in {"queued", "completed", "dry_run"} or result.get("ok"):
        return "done"
    return "failed"


def _run_one(
    item: dict[str, Any],
    *,
    dry_run: bool,
    force: bool,
    notion: NotionClient | None,
    queue: OutboundQueue,
) -> dict[str, Any]:
    started = time.monotonic()
    append_exec_event(
        {
            "event": "started",
            "task_id": item.get("task_id"),
            "channel": item.get("channel"),
            "queue_id": item.get("id"),
            "title": item.get("title"),
        }
    )
    try:
        result = execute_task(
            str(item["task_id"]),
            channel=str(item.get("channel") or "SMS"),
            dry_run=dry_run,
            force=force,
            notion=notion,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("execute failed task=%s", item.get("task_id"))
        result = {"ok": False, "status": "failed", "reason": str(e), "task_id": item.get("task_id")}

    elapsed_ms = int((time.monotonic() - started) * 1000)
    finish_status = _map_finish_status(result)
    notes = result.get("reason") or result.get("notes")
    queue.finish(str(item["id"]), status=finish_status, notes=str(notes) if notes else None, result=result)
    append_exec_event(
        {
            "event": "finished",
            "task_id": item.get("task_id"),
            "channel": item.get("channel"),
            "queue_id": item.get("id"),
            "status": result.get("status") or finish_status,
            "ok": bool(result.get("ok")),
            "elapsed_ms": elapsed_ms,
            "reason": notes,
        }
    )
    return result


def drain_once(
    *,
    dry_run: bool = False,
    force: bool = False,
    notion: NotionClient | None = None,
    queue: OutboundQueue | None = None,
    email_workers: int = 4,
) -> list[dict[str, Any]]:
    """Process queued Email in parallel; one SMS/WA if send gap elapsed."""
    global _last_phone_start_mono
    queue = queue or get_outbound_queue()
    results: list[dict[str, Any]] = []

    emails = queue.pop_emails()
    if emails:
        workers = max(1, min(email_workers, len(emails)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(_run_one, item, dry_run=dry_run, force=force, notion=notion, queue=queue)
                for item in emails
            ]
            for fut in as_completed(futs):
                results.append(fut.result())

    gap = get_send_gap_seconds()
    now_mono = time.monotonic()
    if _last_phone_start_mono and (now_mono - _last_phone_start_mono) < gap:
        return results

    phone = queue.pop_next_phone()
    if not phone:
        return results
    _last_phone_start_mono = time.monotonic()
    results.append(_run_one(phone, dry_run=dry_run, force=force, notion=notion, queue=queue))
    return results


def drain_until_idle(
    *,
    dry_run: bool = False,
    force: bool = False,
    notion: NotionClient | None = None,
    queue: OutboundQueue | None = None,
    max_seconds: float = 3600.0,
) -> list[dict[str, Any]]:
    """Drain queue respecting SMS/WA gaps until empty or timeout (for run-once)."""
    queue = queue or get_outbound_queue()
    deadline = time.monotonic() + max(5.0, max_seconds)
    all_results: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        active = queue.list_items(active_only=True)
        if not any(i.get("status") == "queued" for i in active):
            break
        batch = drain_once(dry_run=dry_run, force=force, notion=notion, queue=queue)
        all_results.extend(batch)
        if not batch:
            # Waiting for phone gap; sleep a bit.
            time.sleep(min(2.0, get_send_gap_seconds()))
    return all_results


def scheduler_tick(
    *,
    channels: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    scan: bool = True,
    drain: bool = True,
) -> dict[str, Any]:
    enqueued: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    if scan:
        enqueued = scan_and_enqueue(channels=channels)
    if drain:
        results = drain_once(dry_run=dry_run, force=force)
    return {
        "scan_interval_seconds": get_scan_interval_seconds(),
        "send_gap_seconds": get_send_gap_seconds(),
        "enqueued": enqueued,
        "results": results,
        "queue_depth": get_outbound_queue().depth(),
    }
