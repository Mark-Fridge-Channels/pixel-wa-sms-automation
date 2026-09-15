from __future__ import annotations

import re
from typing import Any


_EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.I)

DEFAULT_PUBLIC_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "qq.com",
        "163.com",
        "126.com",
        "sina.com",
    }
)


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    # Angle-addr: Name <user@host>
    if "<" in s and ">" in s:
        inner = s[s.rfind("<") + 1 : s.rfind(">")].strip()
        if inner:
            s = inner
    s = s.lower()
    if not _EMAIL_RE.match(s):
        return None
    return s


def email_domain(email: str | None) -> str | None:
    e = normalize_email(email)
    if not e or "@" not in e:
        return None
    return e.rsplit("@", 1)[-1].lower()


def parse_public_domains(spec: str | None) -> set[str]:
    if not spec or not str(spec).strip():
        return set(DEFAULT_PUBLIC_DOMAINS)
    parts = {p.strip().lower() for p in str(spec).split(",") if p.strip()}
    return parts or set(DEFAULT_PUBLIC_DOMAINS)


def is_public_domain(domain: str | None, public_domains: set[str] | None = None) -> bool:
    if not domain:
        return False
    pubs = public_domains if public_domains is not None else set(DEFAULT_PUBLIC_DOMAINS)
    return domain.lower() in pubs


def same_org_domain(
    outbound_email: str | None,
    reply_from: str | None,
    *,
    public_domains: set[str] | None = None,
) -> bool:
    """True if both are non-public and share registrable org domain (incl. subdomain)."""
    d1 = email_domain(outbound_email)
    d2 = email_domain(reply_from)
    if not d1 or not d2:
        return False
    pubs = public_domains if public_domains is not None else set(DEFAULT_PUBLIC_DOMAINS)
    if is_public_domain(d1, pubs) or is_public_domain(d2, pubs):
        return False
    if d1 == d2:
        return True
    # subdomain: sales.acme.com vs acme.com
    return d1.endswith("." + d2) or d2.endswith("." + d1)


def classify_email_reply_relation(
    *,
    outbound_to: str | None,
    reply_from: str | None,
    matched_by: str,
    public_domains: set[str] | None = None,
    allow_cross_domain_thread: bool = True,
) -> dict[str, Any]:
    """Describe how reply From relates to outbound recipient.

    matched_by: gmail_thread | email_exact
    """
    out = normalize_email(outbound_to)
    frm = normalize_email(reply_from)
    pubs = public_domains if public_domains is not None else set(DEFAULT_PUBLIC_DOMAINS)

    if matched_by == "email_exact":
        return {
            "replyMatch": "email_exact",
            "outboundTo": out,
            "replyFrom": frm,
            "proxyReply": False,
            "sameOrgDomain": False,
            "unexpectedSender": False,
            "accept": True,
        }

    # gmail_thread match
    if out and frm and out == frm:
        return {
            "replyMatch": "gmail_thread",
            "outboundTo": out,
            "replyFrom": frm,
            "proxyReply": False,
            "sameOrgDomain": True,
            "unexpectedSender": False,
            "accept": True,
        }

    same_org = same_org_domain(out, frm, public_domains=pubs)
    if same_org:
        return {
            "replyMatch": "gmail_thread",
            "outboundTo": out,
            "replyFrom": frm,
            "proxyReply": True,
            "sameOrgDomain": True,
            "unexpectedSender": False,
            "accept": True,
        }

    # different domain (or missing from) on same thread
    return {
        "replyMatch": "gmail_thread",
        "outboundTo": out,
        "replyFrom": frm,
        "proxyReply": bool(frm and out and frm != out),
        "sameOrgDomain": False,
        "unexpectedSender": True,
        "accept": allow_cross_domain_thread,
    }


def email_value_from_prop(prop: dict | None) -> str | None:
    """Notion email / rich_text / title → normalized email."""
    if not prop:
        return None
    if prop.get("type") == "email" and prop.get("email"):
        return normalize_email(prop.get("email"))
    if prop.get("email"):
        return normalize_email(prop.get("email"))
    chunks = prop.get("rich_text") or prop.get("title") or []
    text = "".join(c.get("plain_text", "") for c in chunks).strip()
    return normalize_email(text) if text else None
