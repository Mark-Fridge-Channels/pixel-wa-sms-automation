from __future__ import annotations

from channel_orchestrator.config import settings
from channel_orchestrator.device_alert import diagnose_phone, maybe_alert_device_health
from channel_orchestrator.heartbeat import touch_heartbeat


def test_diagnose_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "device_heartbeat_stale_minutes", 45)
    touch_heartbeat(
        "phone",
        {
            "google_ok": False,
            "poll_alive": False,
            "a11y_bound": False,
            "automation_on": True,
        },
    )
    reasons = diagnose_phone()
    assert "poll_dead" in reasons
    assert "a11y_unbound" in reasons
    assert "network_or_vpn_down" in reasons


def test_alert_waits_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "alert_sms_to", "+8615810494081")
    monkeypatch.setattr(settings, "alert_unhealthy_grace_minutes", 30)
    monkeypatch.setattr(settings, "alert_sms_cooldown_minutes", 60)
    # No heartbeat file → stale immediately
    r1 = maybe_alert_device_health(force_eval=True)
    assert r1.get("waiting_grace") is True
    r2 = maybe_alert_device_health(force_eval=True)
    assert r2.get("waiting_grace") is True
    assert r2.get("sent") is not True
