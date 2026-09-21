from __future__ import annotations

import time

from channel_orchestrator.config import settings
from channel_orchestrator.wa_probe import (
    STATUS_PENDING,
    STATUS_YES,
    apply_probe_result,
    enqueue_probe,
    get_probe,
    get_probe_by_phone,
)


def test_enqueue_and_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "wa_probe_interval_seconds", 120)
    r1 = enqueue_probe("+15551234567")
    assert r1["ok"] is True
    assert r1["queued"] is True
    probe = r1["probe"]
    assert probe["status"] == STATUS_PENDING
    assert probe["has_whatsapp"] is None
    pid = probe["id"]

    # Same phone while pending → reuse
    r2 = enqueue_probe("+15551234567")
    assert r2["reused"] is True
    assert r2["probe"]["id"] == pid

    # Interval blocks a different phone
    r3 = enqueue_probe("+15559876543")
    assert r3["ok"] is False
    assert r3.get("retry_after_seconds", 0) > 0

    updated = apply_probe_result(pid, has_whatsapp=True, status=STATUS_YES, detail="composer")
    assert updated is not None
    assert updated["has_whatsapp"] is True
    assert get_probe(pid)["status"] == STATUS_YES
    assert get_probe_by_phone("+15551234567")["has_whatsapp"] is True


def test_interval_zero_allows_back_to_back(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "wa_probe_interval_seconds", 0)
    a = enqueue_probe("+8613812345678")
    b = enqueue_probe("+8613812345679")
    assert a["ok"] and b["ok"]
    assert a["probe"]["id"] != b["probe"]["id"]
