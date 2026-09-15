from __future__ import annotations

import json
import logging
import random
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import settings
from .notion_client import NotionClient
from .notion_props import relation_ids, rich_text_plain, select_name
from .notion_props import props as page_props
from .channels import normalize_channel

# Callers may `from channel_orchestrator.scheduler import normalize_channel`
__all__ = [
    "PlanItem",
    "DailyPlan",
    "normalize_channel",
    "build_daily_plan",
    "load_plan",
    "save_plan",
    "due_items",
    "ny_today",
    "schedule_tasks",
    "parse_work_windows",
    "build_candidate_slots",
    "plan_path",
    "interval_for_channel",
]

log = logging.getLogger(__name__)

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


@dataclass
class PlanItem:
    task_id: str
    title: str
    priority: str
    scheduled_at: str
    status: str = "planned"
    channel: str = "SMS"
    phone: str | None = None
    conversation_id: str | None = None
    contact_id: str | None = None
    notes: str | None = None
    job_id: str | None = None


@dataclass
class DailyPlan:
    day: str
    timezone: str
    created_at: str
    channel: str = "SMS"
    items: list[PlanItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "channel": self.channel,
            "timezone": self.timezone,
            "created_at": self.created_at,
            "items": [asdict(i) for i in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailyPlan:
        items = []
        for i in data.get("items") or []:
            allowed = {f.name for f in fields(PlanItem)}
            items.append(PlanItem(**{k: v for k, v in i.items() if k in allowed}))
        return cls(
            day=data["day"],
            channel=data.get("channel") or "SMS",
            timezone=data.get("timezone") or settings.timezone,
            created_at=data.get("created_at") or "",
            items=items,
        )


def parse_work_windows(spec: str) -> list[tuple[time, time]]:
    windows: list[tuple[time, time]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        a, b = part.split("-", 1)
        sh, sm = map(int, a.split(":"))
        eh, em = map(int, b.split(":"))
        windows.append((time(sh, sm), time(eh, em)))
    return windows


def ny_today(now: datetime | None = None, tz_name: str | None = None) -> date:
    tz = ZoneInfo(tz_name or settings.timezone)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    return now.date()


def interval_for_channel(channel: str) -> int:
    if channel.upper() in {"WHATSAPP", "WA"}:
        return settings.wa_min_send_interval_seconds
    return settings.min_send_interval_seconds


def build_candidate_slots(
    day: date,
    *,
    windows: list[tuple[time, time]] | None = None,
    interval_seconds: int | None = None,
    tz_name: str | None = None,
) -> list[datetime]:
    tz = ZoneInfo(tz_name or settings.timezone)
    windows = windows or parse_work_windows(settings.work_windows)
    interval = interval_seconds if interval_seconds is not None else settings.min_send_interval_seconds
    slots: list[datetime] = []
    for start_t, end_t in windows:
        cursor = datetime.combine(day, start_t, tzinfo=tz)
        end = datetime.combine(day, end_t, tzinfo=tz)
        while cursor < end:
            slots.append(cursor)
            cursor += timedelta(seconds=interval)
    return slots


def schedule_tasks(
    tasks: list[dict[str, Any]],
    day: date,
    *,
    channel: str = "SMS",
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[PlanItem]:
    rng = rng or random.Random()
    tz = ZoneInfo(settings.timezone)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    slots = [
        s
        for s in build_candidate_slots(day, interval_seconds=interval_for_channel(channel))
        if s >= now
    ]
    if not slots:
        log.warning("no remaining work slots for %s channel=%s", day, channel)
        return []

    grouped: dict[int, list[dict[str, Any]]] = {0: [], 1: [], 2: []}
    for t in tasks:
        pri = select_name(page_props(t).get("Priority")) or "P2"
        grouped[PRIORITY_ORDER.get(pri, 2)].append(t)

    ordered: list[dict[str, Any]] = []
    for level in (0, 1, 2):
        bucket = grouped[level]
        rng.shuffle(bucket)
        ordered.extend(bucket)

    if len(ordered) > len(slots):
        log.warning(
            "%d tasks cannot fit in remaining windows; leaving unscheduled",
            len(ordered) - len(slots),
        )
        ordered = ordered[: len(slots)]

    chosen_idxs = sorted(rng.sample(range(len(slots)), k=len(ordered)))
    items: list[PlanItem] = []
    for task, idx in zip(ordered, chosen_idxs):
        p = page_props(task)
        pri = select_name(p.get("Priority")) or "P2"
        items.append(
            PlanItem(
                task_id=task["id"],
                title=rich_text_plain(p.get("Follow-up Task")) or task["id"],
                priority=pri,
                scheduled_at=slots[idx].isoformat(),
                status="planned",
                channel=normalize_channel(channel),
                conversation_id=(relation_ids(p.get("Conversations")) or [None])[0],
                contact_id=(relation_ids(p.get("Follow-up Contact")) or [None])[0],
            )
        )
    items.sort(key=lambda x: x.scheduled_at)
    return items


def plan_path(day: date | str, channel: str = "SMS") -> Path:
    d = day if isinstance(day, str) else day.isoformat()
    ch = normalize_channel(channel).lower()
    # legacy SMS path without channel suffix
    if ch == "sms":
        legacy = settings.resolved_data_dir() / f"daily_plan_{d}.json"
        scoped = settings.resolved_data_dir() / f"daily_plan_sms_{d}.json"
        return scoped if scoped.exists() or not legacy.exists() else legacy
    return settings.resolved_data_dir() / f"daily_plan_{ch}_{d}.json"


def save_plan(plan: DailyPlan) -> Path:
    ch = normalize_channel(plan.channel)
    path = settings.resolved_data_dir() / f"daily_plan_{ch.lower()}_{plan.day}.json"
    path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_plan(day: date | str, channel: str = "SMS") -> DailyPlan | None:
    path = plan_path(day, channel)
    if not path.exists():
        return None
    return DailyPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))


def build_daily_plan(
    *,
    notion: NotionClient | None = None,
    day: date | None = None,
    channel: str = "SMS",
    now: datetime | None = None,
    rng: random.Random | None = None,
    merge_existing: bool = True,
) -> DailyPlan:
    notion = notion or NotionClient()
    tz = ZoneInfo(settings.timezone)
    now = now or datetime.now(tz)
    day = day or ny_today(now)
    day_s = day.isoformat()
    ch = normalize_channel(channel)

    pages = notion.query_today_pending(day_s, channel=ch)
    pending_ids = {p["id"] for p in pages}

    kept: list[PlanItem] = []
    if merge_existing:
        existing = load_plan(day, ch)
        if existing:
            for it in existing.items:
                if it.status in {"completed", "failed", "skipped", "in_progress", "queued"}:
                    kept.append(it)
                elif it.status == "planned" and it.task_id in pending_ids:
                    kept.append(it)

    kept_ids = {it.task_id for it in kept}
    fresh_pages = [p for p in pages if p["id"] not in kept_ids]
    new_items = schedule_tasks(fresh_pages, day, channel=ch, rng=rng, now=now)

    merged = {it.task_id: it for it in kept}
    for it in new_items:
        merged[it.task_id] = it

    plan = DailyPlan(
        day=day_s,
        channel=ch,
        timezone=settings.timezone,
        created_at=datetime.now(timezone.utc).isoformat(),
        items=sorted(merged.values(), key=lambda x: x.scheduled_at),
    )
    save_plan(plan)
    return plan


def due_items(plan: DailyPlan, now: datetime | None = None) -> list[PlanItem]:
    tz = ZoneInfo(plan.timezone or settings.timezone)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    due: list[PlanItem] = []
    for it in plan.items:
        if it.status != "planned":
            continue
        when = datetime.fromisoformat(it.scheduled_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        if when <= now:
            due.append(it)
    return due
