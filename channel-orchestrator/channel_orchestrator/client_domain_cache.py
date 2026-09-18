from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .email_util import email_domain, normalize_domain, parse_public_domains
from .notion_client import NotionClient
from .notion_props import props, relation_ids, rich_text_plain

log = logging.getLogger(__name__)


class FollowUpClientDomainCache:
    """domain → [Follow-up Client page ids] from Notion full-table sync."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (settings.resolved_data_dir() / "followup_client_domains.json")
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"updated_at": None, "by_domain": {}, "pages": {}}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._data = {"updated_at": None, "by_domain": {}, "pages": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def lookup(self, domain: str | None) -> list[str]:
        d = normalize_domain(domain)
        if not d:
            return []
        with self._lock:
            ids = self._data.get("by_domain", {}).get(d) or []
            return list(ids)

    def sync(self, notion: NotionClient | None = None) -> dict[str, Any]:
        notion = notion or NotionClient()
        ds = settings.notion_followup_client_ds
        if not ds:
            raise RuntimeError("NOTION_FOLLOWUP_CLIENT_DS 未配置")

        pages = notion.query_data_source(ds, {})
        by_domain: dict[str, list[str]] = {}
        page_meta: dict[str, Any] = {}
        public = parse_public_domains(settings.email_public_domains)

        for page in pages:
            fcid = page.get("id")
            if not fcid:
                continue
            p = props(page)
            title = rich_text_plain(p.get("Follow-up Client")) or fcid
            client_ids = relation_ids(p.get("Client"))
            domains: list[str] = []
            for cid in client_ids:
                try:
                    client = notion.get_page(cid)
                except Exception:  # noqa: BLE001
                    log.exception("failed to load Client %s for Follow-up Client %s", cid, fcid)
                    continue
                domains.extend(extract_domains_from_client(props(client)))

            # unique preserve order
            seen: set[str] = set()
            uniq_domains = []
            for d in domains:
                nd = normalize_domain(d)
                if not nd or nd in seen:
                    continue
                if nd in public:
                    continue  # never map public mailbox domains to a client
                seen.add(nd)
                uniq_domains.append(nd)

            page_meta[fcid] = {"title": title, "domains": uniq_domains}
            for d in uniq_domains:
                by_domain.setdefault(d, [])
                if fcid not in by_domain[d]:
                    by_domain[d].append(fcid)

        with self._lock:
            self._data = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "by_domain": by_domain,
                "pages": page_meta,
            }
            self.save()
            return {
                "ok": True,
                "followup_clients": len(page_meta),
                "domains": len(by_domain),
                "path": str(self.path),
            }


def extract_domains_from_client(client_props: dict[str, Any]) -> list[str]:
    """Best-effort Domain property parse (rich_text / url / title / multi)."""
    prop = client_props.get("Domain")
    if not prop:
        return []
    out: list[str] = []
    if prop.get("type") == "url" and prop.get("url"):
        out.extend(_split_domain_blob(prop["url"]))
    elif prop.get("type") == "rich_text" or prop.get("rich_text"):
        out.extend(_split_domain_blob(rich_text_plain(prop)))
    elif prop.get("type") == "title" or prop.get("title"):
        out.extend(_split_domain_blob(rich_text_plain(prop)))
    elif prop.get("type") == "email" and prop.get("email"):
        d = email_domain(prop.get("email"))
        if d:
            out.append(d)
    elif isinstance(prop.get("formula"), dict):
        f = prop["formula"]
        if f.get("type") == "string" and f.get("string"):
            out.extend(_split_domain_blob(f["string"]))
    else:
        # fallback stringish
        raw = prop.get("url") or prop.get("email") or ""
        if raw:
            out.extend(_split_domain_blob(str(raw)))
        else:
            out.extend(_split_domain_blob(rich_text_plain(prop)))
    return out


def _split_domain_blob(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[\s,;|/]+", str(raw).strip())
    return [p for p in parts if p]


_cache_singleton: FollowUpClientDomainCache | None = None
_cache_lock = threading.Lock()


def get_domain_cache() -> FollowUpClientDomainCache:
    global _cache_singleton
    with _cache_lock:
        if _cache_singleton is None:
            _cache_singleton = FollowUpClientDomainCache()
        return _cache_singleton
