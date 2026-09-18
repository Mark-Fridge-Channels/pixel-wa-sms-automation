from __future__ import annotations

import base64
import json
import logging
import re
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .email_util import extract_emails_from_header_value, normalize_email
from .email_classify import classify_inbound_email

log = logging.getLogger(__name__)

GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
)
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


class GmailClient:
    """Gmail API via OAuth refresh token (single mailbox)."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        user: str | None = None,
        history_path: Path | None = None,
    ) -> None:
        self.client_id = client_id if client_id is not None else settings.gmail_client_id
        self.client_secret = (
            client_secret if client_secret is not None else settings.gmail_client_secret
        )
        self.refresh_token = (
            refresh_token if refresh_token is not None else settings.gmail_refresh_token
        )
        self.user = (user if user is not None else settings.gmail_user) or "me"
        self.history_path = history_path or (
            settings.resolved_data_dir() / "gmail_history_state.json"
        )
        self._lock = threading.Lock()
        self._access_token: str | None = None

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def _ensure_token(self) -> str:
        with self._lock:
            if self._access_token:
                return self._access_token
            if not self.configured():
                raise RuntimeError(
                    "Gmail 未配置：需要 GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN"
                )
            with httpx.Client(timeout=30.0) as client:
                r = client.post(
                    TOKEN_URL,
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "refresh_token": self.refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
                if r.status_code >= 400:
                    raise RuntimeError(f"Gmail token refresh failed: {r.status_code} {r.text[:400]}")
                data = r.json()
                self._access_token = data["access_token"]
                return self._access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{GMAIL_API}/users/{self.user}{path}"
        with httpx.Client(timeout=60.0, headers=self._headers()) as client:
            r = client.request(method, url, **kwargs)
            if r.status_code == 401:
                with self._lock:
                    self._access_token = None
                with httpx.Client(timeout=60.0, headers=self._headers()) as client2:
                    r = client2.request(method, url, **kwargs)
            if r.status_code >= 400:
                raise RuntimeError(f"Gmail {method} {path} -> {r.status_code}: {r.text[:800]}")
            return r.json() if r.content else {}

    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", "/profile")

    def send_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        from_addr = settings.gmail_user or self.user
        if from_addr and from_addr != "me":
            msg["From"] = from_addr
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
        msg.set_content(body or "")

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        return self._request("POST", "/messages/send", json=payload)

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        return self._request("GET", f"/threads/{thread_id}", params={"format": "metadata"})

    def get_message(self, message_id: str, *, format: str = "full") -> dict[str, Any]:
        return self._request("GET", f"/messages/{message_id}", params={"format": format})

    def latest_message_headers(self, thread_id: str) -> dict[str, str]:
        thread = self.get_thread(thread_id)
        messages = thread.get("messages") or []
        if not messages:
            return {}
        last = messages[-1]
        mid = last.get("id")
        if not mid:
            return {}
        full = self.get_message(mid, format="metadata")
        headers = {
            h["name"].lower(): h["value"]
            for h in (full.get("payload") or {}).get("headers") or []
            if h.get("name") and h.get("value")
        }
        message_id = headers.get("message-id") or ""
        refs = headers.get("references") or message_id
        return {
            "message_id_header": message_id,
            "references": refs,
            "subject": headers.get("subject") or "",
            "gmail_message_id": mid,
        }

    def send_new_or_reply(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        gmail_thread_id: str | None = None,
    ) -> dict[str, Any]:
        """New thread if no gmail_thread_id; else reply in thread."""
        in_reply_to = None
        references = None
        subj = subject or "(no subject)"
        if gmail_thread_id:
            hdrs = self.latest_message_headers(gmail_thread_id)
            in_reply_to = hdrs.get("message_id_header") or None
            references = hdrs.get("references") or in_reply_to
            prior = hdrs.get("subject") or ""
            if not subject:
                subj = prior if prior.lower().startswith("re:") else f"Re: {prior or '(no subject)'}"
            elif not subject.lower().startswith("re:"):
                subj = f"Re: {subject}"
        sent = self.send_message(
            to=to,
            subject=subj,
            body=body,
            thread_id=gmail_thread_id,
            in_reply_to=in_reply_to,
            references=references,
        )
        return {
            "gmail_message_id": sent.get("id"),
            "gmail_thread_id": sent.get("threadId") or gmail_thread_id,
            "raw": sent,
        }

    # --- history poll ---

    def _load_history_state(self) -> dict[str, Any]:
        if self.history_path.exists():
            return json.loads(self.history_path.read_text(encoding="utf-8"))
        return {}

    def _save_history_state(self, state: dict[str, Any]) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.history_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.history_path)

    def bootstrap_history(self) -> str:
        profile = self.get_profile()
        hid = str(profile.get("historyId") or "")
        if not hid:
            raise RuntimeError("Gmail profile missing historyId")
        self._save_history_state({"historyId": hid, "updated_at": datetime.now(timezone.utc).isoformat()})
        return hid

    def list_history_message_ids(self, start_history_id: str) -> tuple[list[str], str | None]:
        """Return (message_ids added, new_history_id)."""
        message_ids: list[str] = []
        page_token = None
        newest: str | None = start_history_id
        while True:
            params: dict[str, Any] = {
                "startHistoryId": start_history_id,
                "historyTypes": "messageAdded",
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                data = self._request("GET", "/history", params=params)
            except RuntimeError as e:
                if "404" in str(e) or "historyId" in str(e).lower():
                    log.warning("Gmail historyId expired; re-bootstrap: %s", e)
                    self.bootstrap_history()
                    return [], None
                raise
            for h in data.get("history") or []:
                for added in h.get("messagesAdded") or []:
                    mid = (added.get("message") or {}).get("id")
                    if mid:
                        message_ids.append(mid)
            if data.get("historyId"):
                newest = str(data["historyId"])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        # unique preserve order
        seen: set[str] = set()
        uniq = []
        for mid in message_ids:
            if mid not in seen:
                seen.add(mid)
                uniq.append(mid)
        return uniq, newest

    def poll_new_inbound(self) -> list[dict[str, Any]]:
        """Fetch newly added messages since last historyId; return normalized inbound dicts."""
        if not self.configured():
            log.info("Gmail not configured; skip poll")
            return []
        state = self._load_history_state()
        start = state.get("historyId")
        if not start:
            self.bootstrap_history()
            return []

        ids, newest = self.list_history_message_ids(str(start))
        if newest:
            self._save_history_state(
                {"historyId": newest, "updated_at": datetime.now(timezone.utc).isoformat()}
            )

        our = normalize_email(settings.gmail_user) or normalize_email(self.user)
        results: list[dict[str, Any]] = []
        for mid in ids:
            try:
                parsed = self.parse_inbound_message(mid)
            except Exception:  # noqa: BLE001
                log.exception("failed to parse gmail message %s", mid)
                continue
            if not parsed:
                continue
            if our and normalize_email(parsed.get("sender")) == our:
                continue  # skip our own outbound echoes
            results.append(parsed)
        return results

    def parse_inbound_message(self, message_id: str) -> dict[str, Any] | None:
        msg = self.get_message(message_id, format="full")
        payload = msg.get("payload") or {}
        headers = {
            h["name"].lower(): h["value"]
            for h in payload.get("headers") or []
            if h.get("name") and h.get("value")
        }
        from_raw = headers.get("from") or ""
        to_raw = headers.get("to") or ""
        cc_raw = headers.get("cc") or ""
        _, from_addr = parseaddr(from_raw)
        sender = normalize_email(from_addr) or normalize_email(from_raw)
        to_emails = extract_emails_from_header_value(to_raw)
        cc_emails = extract_emails_from_header_value(cc_raw)
        subject = headers.get("subject") or ""
        body = _extract_plain_body(payload)
        label_ids = msg.get("labelIds") or []
        # Skip SENT-only (we already filter by from); skip drafts
        if "DRAFT" in label_ids:
            return None
        auto_cls = classify_inbound_email(
            sender=sender,
            subject=subject,
            body=body,
            headers=headers,
        )
        internal_ts = msg.get("internalDate")
        received_at = None
        if internal_ts:
            try:
                received_at = datetime.fromtimestamp(int(internal_ts) / 1000, tz=timezone.utc).isoformat()
            except (TypeError, ValueError):
                received_at = None
        return {
            "sender": sender,
            "from_raw": from_raw,
            "to_raw": to_raw,
            "cc_raw": cc_raw,
            "to_emails": to_emails,
            "cc_emails": cc_emails,
            "subject": subject,
            "body": body,
            "message_id": msg.get("id"),
            "gmail_message_id": msg.get("id"),
            "gmail_thread_id": msg.get("threadId"),
            "received_at": received_at,
            "is_auto": not auto_cls.get("is_human"),
            "classify": auto_cls,
            "label_ids": label_ids,
        }


def _extract_plain_body(payload: dict[str, Any]) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _b64url_decode(payload["body"]["data"])
    parts = payload.get("parts") or []
    plain = ""
    html = ""
    for part in parts:
        mime = part.get("mimeType") or ""
        data = (part.get("body") or {}).get("data")
        if mime == "text/plain" and data:
            plain = _b64url_decode(data)
        elif mime == "text/html" and data and not html:
            html = _b64url_decode(data)
        elif part.get("parts"):
            nested = _extract_plain_body(part)
            if nested and not plain:
                plain = nested
    if plain:
        return plain.strip()
    if html:
        return re.sub(r"<[^>]+>", " ", html).strip()
    return ""


def _b64url_decode(data: str) -> str:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad).decode("utf-8", errors="replace")


def _looks_auto_reply(headers: dict[str, str], subject: str, body: str) -> bool:
    """Deprecated: use classify_inbound_email."""
    return not classify_inbound_email(
        sender=headers.get("from"),
        subject=subject,
        body=body,
        headers=headers,
    ).get("is_human", True)


def run_oauth_desktop_flow(
    *,
    client_id: str,
    client_secret: str,
    scopes: tuple[str, ...] = GMAIL_SCOPES,
) -> dict[str, str]:
    """One-time OAuth: opens browser, local redirect, returns tokens dict."""
    import urllib.parse
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    redirect_uri = "http://127.0.0.1:8765/oauth2callback"
    state_holder: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/oauth2callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            state_holder["code"] = (qs.get("code") or [None])[0]
            state_holder["error"] = (qs.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h3>Gmail auth OK. You can close this window.</h3></body></html>")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "hd": "fridgechannels.com",
    }
    if settings.gmail_user:
        params["login_hint"] = settings.gmail_user
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    server = HTTPServer(("127.0.0.1", 8765), Handler)
    print("Open this URL if browser did not open:\n", auth_url)
    webbrowser.open(auth_url)
    while "code" not in state_holder and "error" not in state_holder:
        server.handle_request()
    server.server_close()
    if state_holder.get("error") or not state_holder.get("code"):
        raise RuntimeError(f"OAuth failed: {state_holder.get('error') or 'no code'}")

    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            TOKEN_URL,
            data={
                "code": state_holder["code"],
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if r.status_code >= 400:
            raise RuntimeError(f"token exchange failed: {r.status_code} {r.text[:500]}")
        data = r.json()
    if not data.get("refresh_token"):
        raise RuntimeError(
            "未返回 refresh_token。请在 Google Cloud 撤销该应用访问后重试，并确保 prompt=consent。"
        )
    return {
        "access_token": data.get("access_token") or "",
        "refresh_token": data["refresh_token"],
        "token_type": data.get("token_type") or "Bearer",
        "scope": data.get("scope") or " ".join(scopes),
    }
