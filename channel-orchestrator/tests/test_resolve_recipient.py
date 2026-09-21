from __future__ import annotations

from channel_orchestrator.notion_client import NotionClient


def _page(pid: str, properties: dict) -> dict:
    return {"id": pid, "properties": properties}


def test_resolve_whatsapp_uses_whatsapp_number_not_phone(monkeypatch):
    notion = NotionClient(token="test")

    task = _page(
        "task-1",
        {
            "Follow-up Task": {"type": "title", "title": [{"plain_text": "WA"}]},
            "Priority": {"type": "select", "select": {"name": "P1"}},
            "Scheduled At": {"type": "date", "date": None},
            "Task Status": {"type": "select", "select": {"name": "Pending"}},
            "Channel": {"type": "select", "select": {"name": "WhatsApp"}},
            "Follow-up Contact": {"type": "relation", "relation": [{"id": "contact-1"}]},
            "Conversations": {"type": "relation", "relation": [{"id": "conv-1"}]},
        },
    )
    contact = _page(
        "contact-1",
        {"Key Person": {"type": "relation", "relation": [{"id": "kp-1"}]}},
    )
    kp = _page(
        "kp-1",
        {
            "Phone": {"type": "phone_number", "phone_number": "+15551111111"},
            "WhatsApp Number": {"type": "phone_number", "phone_number": "+15552222222"},
        },
    )
    conv = _page(
        "conv-1",
        {
            "Content": {"type": "rich_text", "rich_text": [{"plain_text": "hello"}]},
            "Thread ID": {"type": "rich_text", "rich_text": [{"plain_text": "wa:+15552222222"}]},
            "Subject": {"type": "rich_text", "rich_text": []},
            "Extended Parameters": {"type": "rich_text", "rich_text": []},
        },
    )
    pages = {"task-1": task, "contact-1": contact, "kp-1": kp, "conv-1": conv}
    monkeypatch.setattr(notion, "get_page", lambda pid: pages[pid])

    resolved = notion.resolve_task_for_send(task)
    assert resolved.resolve_error is None
    assert resolved.phone_e164 == "+15552222222"
    assert resolved.channel == "WHATSAPP"


def test_resolve_sms_still_uses_phone(monkeypatch):
    notion = NotionClient(token="test")

    task = _page(
        "task-1",
        {
            "Follow-up Task": {"type": "title", "title": [{"plain_text": "SMS"}]},
            "Priority": {"type": "select", "select": {"name": "P1"}},
            "Scheduled At": {"type": "date", "date": None},
            "Task Status": {"type": "select", "select": {"name": "Pending"}},
            "Channel": {"type": "select", "select": {"name": "SMS"}},
            "Follow-up Contact": {"type": "relation", "relation": [{"id": "contact-1"}]},
            "Conversations": {"type": "relation", "relation": [{"id": "conv-1"}]},
        },
    )
    contact = _page(
        "contact-1",
        {"Key Person": {"type": "relation", "relation": [{"id": "kp-1"}]}},
    )
    kp = _page(
        "kp-1",
        {
            "Phone": {"type": "phone_number", "phone_number": "+15551111111"},
            "WhatsApp Number": {"type": "phone_number", "phone_number": "+15552222222"},
        },
    )
    conv = _page(
        "conv-1",
        {
            "Content": {"type": "rich_text", "rich_text": [{"plain_text": "hello"}]},
            "Thread ID": {"type": "rich_text", "rich_text": [{"plain_text": "sms:+15551111111"}]},
            "Subject": {"type": "rich_text", "rich_text": []},
            "Extended Parameters": {"type": "rich_text", "rich_text": []},
        },
    )
    pages = {"task-1": task, "contact-1": contact, "kp-1": kp, "conv-1": conv}
    monkeypatch.setattr(notion, "get_page", lambda pid: pages[pid])

    resolved = notion.resolve_task_for_send(task)
    assert resolved.resolve_error is None
    assert resolved.phone_e164 == "+15551111111"
    assert resolved.channel == "SMS"
