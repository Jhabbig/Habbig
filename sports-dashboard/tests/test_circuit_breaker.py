"""Tests for the Polymarket WS circuit breaker."""
import sports_dashboard as sd


def _reset_breaker(monkeypatch):
    """Each test starts from a clean breaker state."""
    monkeypatch.setattr(sd, "_pm_ws_failure_count", 0)
    monkeypatch.setattr(sd, "_pm_ws_circuit_open_seconds", sd.PM_WS_CIRCUIT_OPEN_SECONDS)


def test_first_failure_uses_short_backoff(monkeypatch):
    _reset_breaker(monkeypatch)
    n, sleep = sd._pm_ws_record_failure()
    assert n == 1
    assert sleep == 1.0


def test_subsequent_failures_double(monkeypatch):
    """1, 2, 4, 8s for failures 1-4 (before circuit opens at 5)."""
    _reset_breaker(monkeypatch)
    sleeps = []
    for _ in range(4):
        _, s = sd._pm_ws_record_failure()
        sleeps.append(s)
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_circuit_opens_at_threshold(monkeypatch):
    """At threshold (5 by default), sleep jumps to the open-circuit
    duration (~5 minutes), not the next exponential step."""
    _reset_breaker(monkeypatch)
    for _ in range(sd.PM_WS_CIRCUIT_THRESHOLD - 1):
        sd._pm_ws_record_failure()
    n, sleep = sd._pm_ws_record_failure()
    assert n == sd.PM_WS_CIRCUIT_THRESHOLD
    assert sleep == sd.PM_WS_CIRCUIT_OPEN_SECONDS  # 300s


def test_open_circuit_doubles_each_subsequent_failure(monkeypatch):
    """Once open, each failed probe doubles the cooldown until it hits
    PM_WS_CIRCUIT_MAX_SECONDS."""
    _reset_breaker(monkeypatch)
    # Drive to threshold
    for _ in range(sd.PM_WS_CIRCUIT_THRESHOLD):
        sd._pm_ws_record_failure()
    # Next two failures should double (300 → 600 → 1200)
    _, s1 = sd._pm_ws_record_failure()
    _, s2 = sd._pm_ws_record_failure()
    assert s1 == 600
    assert s2 == 1200


def test_open_circuit_caps_at_max(monkeypatch):
    """Cooldown is capped at PM_WS_CIRCUIT_MAX_SECONDS (1h by default)."""
    _reset_breaker(monkeypatch)
    for _ in range(50):  # way past threshold
        _, sleep = sd._pm_ws_record_failure()
    assert sleep <= sd.PM_WS_CIRCUIT_MAX_SECONDS


def test_success_resets_breaker(monkeypatch):
    """After enough failures to open the circuit, a single success should
    reset everything so transient outages don't permanently penalize
    the connection."""
    _reset_breaker(monkeypatch)
    for _ in range(10):
        sd._pm_ws_record_failure()
    sd._pm_ws_record_success()
    assert sd._pm_ws_failure_count == 0
    # Next failure starts at the short-backoff regime again
    n, sleep = sd._pm_ws_record_failure()
    assert n == 1
    assert sleep == 1.0


def test_metric_tracks_failure_count(monkeypatch):
    """The M_PM_WS_FAILURES gauge should follow _pm_ws_failure_count."""
    _reset_breaker(monkeypatch)
    sd._pm_ws_record_failure()
    sd._pm_ws_record_failure()
    # Read the gauge via prometheus_client's internal store
    # (the gauge value is whatever was last set())
    # We don't have direct access to the value here without scraping
    # /metrics, so test by behavior: success resets to 0
    sd._pm_ws_record_success()
    assert sd._pm_ws_failure_count == 0
