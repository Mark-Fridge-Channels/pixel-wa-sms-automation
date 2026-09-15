from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from .config import settings


class SmsGatewayClient:
    """Client for SMS Gateway for Android (capcom6) REST API.

    Modes (SMS_GATEWAY_MODE):
    - local:   phone Local Server  → POST {base}/message
    - private: self-hosted server  → POST {base}/api/3rdparty/v1/messages
    - cloud:   api.sms-gate.app    → POST {base}/3rdparty/v1/messages

    Uses urllib instead of httpx: Local Server over adb forward often returns
    HTTP 502 to httpx (Accept-Encoding / keep-alive quirks).
    """

    def __init__(self) -> None:
        self.base = settings.sms_gateway_url.rstrip("/")
        self.mode = (settings.sms_gateway_mode or "local").strip().lower()
        self.auth = None
        if settings.sms_gateway_user:
            self.auth = (settings.sms_gateway_user, settings.sms_gateway_password)

    def _auth_header(self) -> dict[str, str]:
        if not self.auth:
            return {}
        token = base64.b64encode(f"{self.auth[0]}:{self.auth[1]}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _api_prefix(self) -> str:
        if self.mode == "private":
            return "/api/3rdparty/v1"
        if self.mode == "cloud":
            return "/3rdparty/v1"
        return ""

    def _send_path(self) -> str:
        if self.mode in {"private", "cloud"}:
            return f"{self._api_prefix()}/messages"
        return "/message"

    def _webhooks_path(self) -> str:
        if self.mode in {"private", "cloud"}:
            return f"{self._api_prefix()}/webhooks"
        return "/webhooks"

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, str]:
        data = None
        headers = {"Accept": "*/*", "Connection": "close", **self._auth_header()}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"SMS Gateway {method} {path} -> {e.code}: {body[:500]}") from e

    def ping(self) -> dict[str, Any]:
        last = "unreachable"
        candidates = ["/health", "/", "/api/v1/health"]
        if self.mode == "private":
            candidates = ["/health", "/api/3rdparty/v1/health", "/"]
        elif self.mode == "cloud":
            candidates = ["/health", "/3rdparty/v1/health", "/"]
        for path in candidates:
            try:
                status, body = self._request("GET", path, timeout=15.0)
                if status < 500:
                    return {
                        "ok": True,
                        "mode": self.mode,
                        "path": path,
                        "status": status,
                        "body": body[:500],
                        "host": urlparse(self.base).netloc,
                    }
                last = f"{path}:{status}"
            except Exception as e:  # noqa: BLE001
                last = str(e)
        return {"ok": False, "mode": self.mode, "error": last}

    def send_sms(self, phone_numbers: list[str], message: str, sim_number: int | None = None) -> dict[str, Any]:
        """Send SMS. Local uses /message; private/cloud use .../messages."""
        if sim_number is None:
            sim_number = settings.sms_sim_number
        payload: dict[str, Any] = {
            "textMessage": {"text": message},
            "phoneNumbers": phone_numbers,
        }
        if sim_number is not None:
            payload["simNumber"] = sim_number
        path = self._send_path()
        status, body = self._request("POST", path, payload=payload, timeout=60.0)
        if status >= 400:
            raise RuntimeError(f"SMS Gateway POST {path} -> {status}: {body[:500]}")
        if body.strip():
            return json.loads(body)
        return {"status_code": status}

    def list_webhooks(self) -> Any:
        status, body = self._request("GET", self._webhooks_path(), timeout=30.0)
        if status >= 400:
            raise RuntimeError(f"SMS Gateway GET webhooks -> {status}: {body[:500]}")
        return json.loads(body) if body.strip() else []

    def register_webhook(self, url: str, event: str = "sms:received") -> Any:
        payload = {"id": None, "url": url, "event": event}
        # Local API historically accepted without id; private may require fields.
        status, body = self._request(
            "POST",
            self._webhooks_path(),
            payload={"url": url, "event": event},
            timeout=30.0,
        )
        if status >= 400:
            # retry legacy shape
            status, body = self._request(
                "POST", self._webhooks_path(), payload=payload, timeout=30.0
            )
        if status >= 400:
            raise RuntimeError(f"SMS Gateway POST webhooks -> {status}: {body[:500]}")
        return json.loads(body) if body.strip() else {"status_code": status}
