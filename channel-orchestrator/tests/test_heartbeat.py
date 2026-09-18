from __future__ import annotations

import json

from channel_orchestrator.config import settings
from channel_orchestrator.heartbeat import heartbeat_stale, read_heartbeat, touch_heartbeat


def test_touch_heartbeat_merges_vpn_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    touch_heartbeat("phone", {"google_ok": False, "vpn_transport": False})
    first = read_heartbeat()["phone"]
    assert first["google_ok"] is False
    assert first["vpn_transport"] is False
    assert "last_seen" in first

    touch_heartbeat("phone")
    second = read_heartbeat()["phone"]
    assert second["google_ok"] is False
    assert second["vpn_transport"] is False
    assert second["last_seen"] >= first["last_seen"]

    touch_heartbeat("phone", {"google_ok": True, "vpn_transport": True, "always_on_vpn": False})
    third = read_heartbeat()["phone"]
    assert third["google_ok"] is True
    assert third["vpn_transport"] is True
    assert third["always_on_vpn"] is False


def test_heartbeat_stale_without_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    assert heartbeat_stale("phone", minutes=1) is True
    touch_heartbeat("phone")
    assert heartbeat_stale("phone", minutes=45) is False
    dumped = json.loads((tmp_path / "device_heartbeat.json").read_text(encoding="utf-8"))
    assert "phone" in dumped
