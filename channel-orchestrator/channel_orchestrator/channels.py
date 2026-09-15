from __future__ import annotations


def normalize_channel(channel: str) -> str:
    c = (channel or "SMS").strip().upper()
    if c in {"WA", "WHATSAPP"}:
        return "WHATSAPP"
    if c in {"EMAIL", "MAIL", "GMAIL"}:
        return "EMAIL"
    if c == "SMS":
        return "SMS"
    return c


def channel_display_name(channel: str) -> str:
    ch = normalize_channel(channel)
    return {"WHATSAPP": "WhatsApp", "EMAIL": "Email", "SMS": "SMS"}.get(ch, ch.title())
