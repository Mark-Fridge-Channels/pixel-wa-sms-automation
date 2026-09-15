from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

from .config import settings


class SmsGatewayClient:
    """Client for SMS Gateway for Android (capcom6) style Basic-auth REST API.

    Uses urllib instead of httpx: this device's local SMS Gateway often returns
    HTTP 502 to httpx (Accept-Encoding / keep-alive quirks over adb forward).
    """

    def __init__(self) -> None:
        self.base = settings.sms_gateway_url.rstrip("/")
        self.auth = None
        if settings.sms_gateway_user:
            self.auth = (settings.sms_gateway_user, settings.sms_gateway_password)

    def _auth_header(self) -> dict[str, str]:
        if not self.auth:
            return {}
        token = base64.b64encode(f"{self.auth[0]}:{self.auth[1]}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

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
        for path in ("/health", "/", "/api/v1/health"):
            try:
                status, body = self._request("GET", path, timeout=15.0)
                if status < 500:
                    return {"ok": True, "path": path, "status": status, "body": body[:500]}
                last = f"{path}:{status}"
            except Exception as e:  # noqa: BLE001
                last = str(e)
        return {"ok": False, "error": last}

    def send_sms(self, phone_numbers: list[str], message: str, sim_number: int | None = None) -> dict[str, Any]:
        """
        POST /message
        Docs: https://docs.sms-gate.app/
        On this Pixel, Saily #2 is simNumber=2.
        """
        if sim_number is None:
            sim_number = settings.sms_sim_number
        payload: dict[str, Any] = {
            "textMessage": {"text": message},
            "phoneNumbers": phone_numbers,
        }
        if sim_number is not None:
            payload["simNumber"] = sim_number
        status, body = self._request("POST", "/message", payload=payload, timeout=60.0)
        if status >= 400:
            raise RuntimeError(f"SMS Gateway POST /message -> {status}: {body[:500]}")
        if body.strip():
            return json.loads(body)
        return {"status_code": status}
