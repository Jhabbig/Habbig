"""Tests for the /metrics endpoint and metric-emission shape."""
from fastapi.testclient import TestClient

import sports_dashboard as sd


def test_metrics_endpoint_returns_prometheus_format():
    client = TestClient(sd.app)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "# HELP" in body
    assert "# TYPE" in body


def test_metrics_endpoint_exposes_dashboard_metrics():
    """All seven dashboard-specific metrics must appear in /metrics output."""
    # Touch each metric so it shows up in the registry output.
    sd.M_COMPARISONS.labels(sport="basketball_nba").inc(0)
    sd.M_SIGNALS.labels(sport="basketball_nba").inc(0)
    sd.M_POLL_ERRORS.labels(stage="test").inc(0)
    sd.M_ALERT_SEND.labels(channel="email", result="ok").inc(0)
    sd.M_ODDS_REMAINING.set(0)
    sd.M_ODDS_USED.set(0)
    sd.M_MATCH_REJECTS.labels(reason="test").inc(0)
    sd.M_POLL_DURATION.observe(0.1)

    client = TestClient(sd.app)
    body = client.get("/metrics").text
    for name in [
        "sports_dashboard_poll_loop_seconds",
        "sports_dashboard_comparisons_total",
        "sports_dashboard_signals_total",
        "sports_dashboard_poll_errors_total",
        "sports_dashboard_alert_send_total",
        "sports_dashboard_odds_api_remaining",
        "sports_dashboard_odds_api_used",
        "sports_dashboard_match_rejects_total",
    ]:
        assert name in body, f"{name} missing from /metrics output"
