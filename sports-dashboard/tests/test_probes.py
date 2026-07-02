"""Tests for /healthz, /readyz, and /changelog."""
from fastapi.testclient import TestClient

import sports_dashboard as sd


def _client():
    return TestClient(sd.app)


def test_healthz_returns_200():
    """Health check must succeed even when external APIs are down."""
    r = _client().get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "sports-dashboard"
    assert "polymarket_ws_failures" in body
    assert "odds_quota_remaining" in body


def test_healthz_reports_pm_failures(monkeypatch):
    """Surfacing the WS failure count is useful for LB / dashboards
    that want to see when the circuit breaker is open."""
    monkeypatch.setattr(sd, "_pm_ws_failure_count", 7)
    r = _client().get("/healthz")
    assert r.json()["polymarket_ws_failures"] == 7


def test_readyz_returns_503_before_data_updater_runs():
    """A fresh process hasn't run the data updater yet — readiness
    probe should be 503 so the LB doesn't route to it."""
    # Clear any prior last_update from earlier tests
    original = sd.dashboard_data.get("last_update")
    sd.dashboard_data["last_update"] = None
    try:
        r = _client().get("/readyz")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not_ready"
        assert any("data_updater" in i for i in body["issues"])
    finally:
        sd.dashboard_data["last_update"] = original


def test_readyz_returns_200_after_data_updater():
    """Once the data updater has run, readiness flips to 200."""
    original = sd.dashboard_data.get("last_update")
    sd.dashboard_data["last_update"] = "2026-05-21T10:00:00+00:00"
    try:
        r = _client().get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"
        assert r.json()["issues"] == []
    finally:
        sd.dashboard_data["last_update"] = original


def test_changelog_page_is_public():
    """Changelog is conversion content — anonymous-readable."""
    r = _client().get("/changelog")
    assert r.status_code == 200
    assert "What" in r.text
