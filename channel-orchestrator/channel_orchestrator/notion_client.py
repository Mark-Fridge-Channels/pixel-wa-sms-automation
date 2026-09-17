from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .channels import channel_display_name, normalize_channel
from .config import settings
from .email_util import email_value_from_prop, normalize_email
from .notion_props import (
    date_start,
    is_scheduled_due,
    parse_notion_datetime,
    phone_value,
    props,
    relation_ids,
    rich_text_plain,
    select_name,
)
from .phone import normalize_e164_with_reason

log = logging.getLogger(__name__)

NOTION_VERSION = "2025-09-03"


@dataclass
class ResolvedTask:
    task_id: str
    title: str
    priority: str
    scheduled_at: str | None
    status: str | None
    contact_id: str | None
    keyperson_id: str | None
    phone_e164: str | None
    conversation_id: str | None
    content: str | None
    thread_id: str | None = None
    channel: str = "SMS"
    email: str | None = None
    subject: str | None = None
    extended_parameters: dict[str, Any] = field(default_factory=dict)
    resolve_error: str | None = None


class NotionClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token if token is not None else settings.notion_token
        self.task_ds = settings.notion_task_ds
        self.conversation_ds = settings.notion_conversation_ds
        self.conversation_db = settings.notion_conversation_db

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("NOTION_TOKEN 未配置")
        url = f"https://api.notion.com/v1{path}"
        with httpx.Client(timeout=60.0, headers=self._headers()) as client:
            r = client.request(method, url, **kwargs)
            if r.status_code >= 400:
                raise RuntimeError(f"Notion {method} {path} -> {r.status_code}: {r.text[:800]}")
            return r.json() if r.content else {}

    def get_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")

    def query_data_source(self, data_source_id: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        payload = dict(body)
        while True:
            data = self._request("POST", f"/data_sources/{data_source_id}/query", json=payload)
            results.extend(data.get("results") or [])
            if not data.get("has_more"):
                break
            payload["start_cursor"] = data.get("next_cursor")
        return results

    def query_today_pending(self, day_yyyy_mm_dd: str, channel: str = "SMS") -> list[dict[str, Any]]:
        """Legacy: Pending + Channel + Scheduled At equals calendar day (date-only)."""
        ch = channel_display_name(channel)
        filt = {
            "and": [
                {"property": "Channel", "select": {"equals": ch}},
                {"property": "Task Status", "status": {"equals": "Pending"}},
                {"property": "Scheduled At", "date": {"equals": day_yyyy_mm_dd}},
            ]
        }
        return self.query_data_source(self.task_ds, {"filter": filt})

    def query_today_sms_pending(self, day_yyyy_mm_dd: str) -> list[dict[str, Any]]:
        return self.query_today_pending(day_yyyy_mm_dd, channel="SMS")

    def query_due_pending(
        self,
        *,
        channels: list[str] | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Pending tasks whose Scheduled At (with time+tz) is <= now.

        Notion filter uses on_or_before as a coarse gate; final due check is
        client-side with timezone-aware parsing.
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        chans = channels or ["SMS", "WHATSAPP", "EMAIL"]
        channel_filters = [
            {"property": "Channel", "select": {"equals": channel_display_name(c)}} for c in chans
        ]
        # Coarse gate: include anything on_or_before "now" ISO (Notion compares dates).
        filt: dict[str, Any] = {
            "and": [
                {"property": "Task Status", "status": {"equals": "Pending"}},
                {"or": channel_filters} if len(channel_filters) > 1 else channel_filters[0],
                {
                    "property": "Scheduled At",
                    "date": {"on_or_before": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                },
            ]
        }
        pages = self.query_data_source(self.task_ds, {"filter": filt})
        due: list[dict[str, Any]] = []
        for page in pages:
            p = props(page)
            if not is_scheduled_due(p.get("Scheduled At"), now=now, default_tz=settings.timezone):
                continue
            due.append(page)

        priority_order = {"P0": 0, "P1": 1, "P2": 2}

        def sort_key(page: dict[str, Any]) -> tuple:
            p = props(page)
            scheduled = parse_notion_datetime(p.get("Scheduled At"), default_tz=settings.timezone)
            ts = scheduled.astimezone(timezone.utc).timestamp() if scheduled else 0.0
            pri = priority_order.get(select_name(p.get("Priority")) or "P2", 9)
            return (ts, pri, page.get("id") or "")

        due.sort(key=sort_key)
        return due

    def get_task_status(self, task_id: str) -> str | None:
        page = self.get_page(task_id)
        return select_name(props(page).get("Task Status"))

    def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        ended_at: datetime | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {"Task Status": {"status": {"name": status}}}
        if ended_at is not None:
            # Notion date with time
            properties["Ended At"] = {
                "date": {"start": ended_at.astimezone(timezone.utc).isoformat()}
            }
        if notes is not None:
            properties["Notes"] = {"rich_text": [{"text": {"content": notes[:1900]}}]}
        return self._request("PATCH", f"/pages/{task_id}", json={"properties": properties})

    def update_conversation_outbound(
        self,
        conversation_id: str,
        *,
        message_status: str,
        interaction_at: datetime,
        sender: str | None,
        message_id: str | None,
        thread_id: str | None,
    ) -> dict[str, Any]:
        page = self.get_page(conversation_id)
        p = props(page)
        properties: dict[str, Any] = {
            "Message Status": {"select": {"name": message_status}},
            "Interaction At": {
                "date": {"start": interaction_at.astimezone(timezone.utc).isoformat()}
            },
        }
        if sender and not rich_text_plain(p.get("Sender")):
            properties["Sender"] = {"rich_text": [{"text": {"content": sender}}]}
        if message_id:
            properties["Message ID"] = {"rich_text": [{"text": {"content": message_id}}]}
        existing_thread = rich_text_plain(p.get("Thread ID"))
        if thread_id and not existing_thread:
            properties["Thread ID"] = {"rich_text": [{"text": {"content": thread_id}}]}
        return self._request("PATCH", f"/pages/{conversation_id}", json={"properties": properties})

    def update_conversation_extended_parameters(
        self,
        conversation_id: str,
        extended: dict[str, Any],
        *,
        merge: bool = True,
    ) -> dict[str, Any]:
        page = self.get_page(conversation_id)
        existing = parse_extended_parameters(props(page).get("Extended Parameters"))
        merged = {**existing, **extended} if merge else dict(extended)
        text = json.dumps(merged, ensure_ascii=False)
        properties = {
            "Extended Parameters": {"rich_text": [{"text": {"content": text[:1900]}}]}
        }
        return self._request("PATCH", f"/pages/{conversation_id}", json={"properties": properties})

    def create_inbound_conversation(
        self,
        *,
        content: str,
        sender: str,
        interaction_at: datetime,
        thread_id: str,
        message_id: str | None,
        task_id: str | None,
        contact_id: str | None,
        title: str,
        channel: str = "SMS",
        extended_parameters: dict[str, Any] | None = None,
        subject: str | None = None,
    ) -> dict[str, Any]:
        ch_name = channel_display_name(channel)
        properties: dict[str, Any] = {
            "Conversation Record": {"title": [{"text": {"content": title[:200]}}]},
            "Channel": {"select": {"name": ch_name}},
            "Direction": {"select": {"name": "Inbound"}},
            "Content": {"rich_text": [{"text": {"content": (content or "")[:1900]}}]},
            "Sender": {"rich_text": [{"text": {"content": sender}}]},
            "Message Status": {"select": {"name": "Received"}},
            "Reply Status": {"select": {"name": "Needs Reply"}},
            "Interaction At": {
                "date": {"start": interaction_at.astimezone(timezone.utc).isoformat()}
            },
            "Thread ID": {"rich_text": [{"text": {"content": thread_id}}]},
        }
        if message_id:
            properties["Message ID"] = {"rich_text": [{"text": {"content": message_id}}]}
        if subject:
            properties["Subject"] = {"rich_text": [{"text": {"content": subject[:500]}}]}
        if extended_parameters:
            text = json.dumps(extended_parameters, ensure_ascii=False)
            properties["Extended Parameters"] = {
                "rich_text": [{"text": {"content": text[:1900]}}]
            }
        if task_id:
            properties["Follow-up Task"] = {"relation": [{"id": task_id}]}
        if contact_id:
            properties["Follow-up Contact"] = {"relation": [{"id": contact_id}]}

        body: dict[str, Any] = {"properties": properties}
        try:
            body["parent"] = {"type": "data_source_id", "data_source_id": self.conversation_ds}
            return self._request("POST", "/pages", json=body)
        except RuntimeError as e:
            log.warning("create with data_source_id failed, retry database_id: %s", e)
            body["parent"] = {"database_id": self.conversation_db}
            return self._request("POST", "/pages", json=body)

    def resolve_task_for_send(self, task_page: dict[str, Any] | str) -> ResolvedTask:
        if isinstance(task_page, str):
            page = self.get_page(task_page)
        else:
            page = task_page
        p = props(page)
        task_id = page["id"]
        title = rich_text_plain(p.get("Follow-up Task")) or task_id
        priority = select_name(p.get("Priority")) or "P2"
        scheduled = date_start(p.get("Scheduled At"))
        status = select_name(p.get("Task Status"))
        channel = normalize_channel(select_name(p.get("Channel")) or "SMS")
        contact_ids = relation_ids(p.get("Follow-up Contact"))
        conv_ids = relation_ids(p.get("Conversations"))

        contact_id = contact_ids[0] if contact_ids else None
        conversation_id = conv_ids[0] if conv_ids else None
        if len(conv_ids) > 1:
            log.warning("task %s has %d conversations; using first %s", task_id, len(conv_ids), conversation_id)

        phone_e164: str | None = None
        email: str | None = None
        keyperson_id: str | None = None
        content: str | None = None
        thread_id: str | None = None
        subject: str | None = None
        extended: dict[str, Any] = {}
        error: str | None = None

        if not contact_id:
            error = "缺少 Follow-up Contact，无法解析收件人"
        else:
            contact = self.get_page(contact_id)
            kp_ids = relation_ids(props(contact).get("Key Person"))
            keyperson_id = kp_ids[0] if kp_ids else None
            if not keyperson_id:
                error = "Follow-up Contact 未关联 Key Person"
            else:
                kp = self.get_page(keyperson_id)
                kp_props = props(kp)
                if channel == "EMAIL":
                    raw_email = (
                        email_value_from_prop(kp_props.get("Email"))
                        or email_value_from_prop(kp_props.get("E-mail"))
                        or normalize_email(rich_text_plain(kp_props.get("Email")))
                    )
                    email = raw_email
                    if not email:
                        error = f"Key Person 邮箱无效或为空：{kp_props.get('Email')!r}"
                else:
                    raw_phone = phone_value(kp_props.get("Phone"))
                    phone_e164, phone_err = normalize_e164_with_reason(raw_phone)
                    if not phone_e164:
                        error = phone_err or f"手机格式识别失败：原始={raw_phone!r}"

        if not conversation_id:
            error = (error + "；" if error else "") + "缺少关联 Conversation，无发送正文"
        else:
            conv = self.get_page(conversation_id)
            cp = props(conv)
            content = rich_text_plain(cp.get("Content"))
            thread_id = rich_text_plain(cp.get("Thread ID")) or None
            subject = rich_text_plain(cp.get("Subject")) or None
            if not subject and channel == "EMAIL":
                subject = title
            extended = parse_extended_parameters(cp.get("Extended Parameters"))
            if not content:
                error = (error + "；" if error else "") + "Conversation Content 为空"
            if not thread_id:
                error = (error + "；" if error else "") + "Conversation 缺少 Thread ID（须由 Task/Conversation 携带，不可本地生成）"

        return ResolvedTask(
            task_id=task_id,
            title=title,
            priority=priority,
            scheduled_at=scheduled,
            status=status,
            contact_id=contact_id,
            keyperson_id=keyperson_id,
            phone_e164=phone_e164,
            conversation_id=conversation_id,
            content=content,
            thread_id=thread_id,
            channel=channel,
            email=email,
            subject=subject,
            extended_parameters=extended,
            resolve_error=error,
        )

    def summarize_task(self, page: dict[str, Any]) -> dict[str, Any]:
        p = props(page)
        return {
            "id": page["id"],
            "title": rich_text_plain(p.get("Follow-up Task")),
            "priority": select_name(p.get("Priority")) or "P2",
            "status": select_name(p.get("Task Status")),
            "scheduled_at": date_start(p.get("Scheduled At")),
            "channel": select_name(p.get("Channel")),
            "contact_ids": relation_ids(p.get("Follow-up Contact")),
            "conversation_ids": relation_ids(p.get("Conversations")),
        }


def parse_extended_parameters(prop: dict[str, Any] | None) -> dict[str, Any]:
    if not prop:
        return {}
    # Notion JSON property (if present)
    if prop.get("type") == "json" and isinstance(prop.get("json"), dict):
        return dict(prop["json"])
    raw = rich_text_plain(prop)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        log.warning("Extended Parameters is not valid JSON: %s", raw[:120])
        return {}
