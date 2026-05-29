"""Tests for the startup config check and /api/diagnostics/config-check."""
from fastapi.testclient import TestClient

import sports_dashboard as sd


def _client():
    return TestClient(sd.app)


# ── _config_check ───────────────────────────────────────────────────────────

def test_config_check_returns_list_of_items():
    items = sd._config_check()
    assert isinstance(items, list)
    assert len(items) > 0
    for item in items:
        assert "key" in item
        assert "status" in item
        assert item["status"] in ("ok", "warn", "fail")


def test_config_check_flags_missing_odds_api_key(monkeypatch):
    monkeypatch.setattr(sd, "ODDS_API_KEY", "")
    items = sd._config_check()
    odds = next(i for i in items if i["key"] == "ODDS_API_KEY")
    assert odds["status"] == "warn"
    assert "the-odds-api.com" in odds["remediation"]


def test_config_check_passes_when_odds_api_key_set(monkeypatch):
    monkeypatch.setattr(sd, "ODDS_API_KEY", "test-key")
    items = sd._config_check()
    odds = next(i for i in items if i["key"] == "ODDS_API_KEY")
    assert odds["status"] == "ok"


def test_config_check_flags_missing_vapid(monkeypatch):
    monkeypatch.setattr(sd, "VAPID_PUBLIC_KEY", "")
    monkeypatch.setattr(sd, "VAPID_PRIVATE_KEY", "")
    items = sd._config_check()
    vapid = next(i for i in items if "VAPID" in i["key"])
    assert vapid["status"] == "warn"
    assert "pywebpush vapid_key" in vapid["remediation"]


def test_config_check_flags_missing_anthropic_key(monkeypatch):
    monkeypatch.setattr(sd, "ANTHROPIC_API_KEY", "")
    items = sd._config_check()
    a = next(i for i in items if i["key"] == "ANTHROPIC_API_KEY")
    assert a["status"] == "warn"


def test_config_check_fail_when_no_auth_and_no_dev_mode(monkeypatch):
    """Production deploy with no gateway secret AND no DEV_MODE is a
    HARD failure — every request will 503."""
    monkeypatch.setattr(sd, "_BEHIND_GATEWAY", False)
    monkeypatch.setattr(sd, "_DEV_MODE", False)
    items = sd._config_check()
    auth = next(i for i in items if i["key"] == "GATEWAY_SSO_SECRET")
    assert auth["status"] == "fail"


def test_config_check_warns_in_dev_mode(monkeypatch):
    """DEV_MODE is fine for local but should be flagged as warn —
    nobody wants to ship a prod with DEV_MODE accidentally on."""
    monkeypatch.setattr(sd, "_BEHIND_GATEWAY", False)
    monkeypatch.setattr(sd, "_DEV_MODE", True)
    items = sd._config_check()
    auth = next(i for i in items if i["key"] == "DEV_MODE")
    assert auth["status"] == "warn"


def test_config_check_flags_open_ws_circuit(monkeypatch):
    """When the WS circuit breaker is OPEN, surface that as a fail
    in the config check too — it's a runtime config issue (network
    or Polymarket-side) the operator should see."""
    monkeypatch.setattr(sd, "_pm_ws_failure_count", sd.PM_WS_CIRCUIT_THRESHOLD + 1)
    items = sd._config_check()
    ws = next(i for i in items if i["key"] == "polymarket_ws")
    assert ws["status"] == "fail"


def test_config_check_passes_when_ws_healthy(monkeypatch):
    monkeypatch.setattr(sd, "_pm_ws_failure_count", 0)
    items = sd._config_check()
    ws = next(i for i in items if i["key"] == "polymarket_ws")
    assert ws["status"] == "ok"


# ── /api/diagnostics/config-check endpoint ──────────────────────────────────

def test_endpoint_returns_structured_payload():
    r = _client().get("/api/diagnostics/config-check")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "n_total" in body
    assert "n_fail" in body
    assert "n_warn" in body
    assert body["n_total"] == len(body["items"])


def test_endpoint_counts_match_items():
    body = _client().get("/api/diagnostics/config-check").json()
    actual_fail = sum(1 for i in body["items"] if i["status"] == "fail")
    actual_warn = sum(1 for i in body["items"] if i["status"] == "warn")
    assert body["n_fail"] == actual_fail
    assert body["n_warn"] == actual_warn


# ── _print_config_check ─────────────────────────────────────────────────────

def test_print_silent_when_all_ok(monkeypatch, capsys):
    """No issues → one line confirming all nominal, no noisy details."""
    monkeypatch.setattr(
        sd, "_config_check",
        lambda: [{"key": "X", "status": "ok", "effect": "", "remediation": ""}],
    )
    sd._print_config_check()
    out = capsys.readouterr().out
    assert "all systems nominal" in out
    assert "FAIL" not in out and "WARN" not in out


def test_print_lists_each_issue(monkeypatch, capsys):
    monkeypatch.setattr(sd, "_config_check", lambda: [
        {"key": "GATEWAY_SSO_SECRET", "status": "fail",
         "effect": "no auth", "remediation": "set the env var"},
        {"key": "ANTHROPIC_API_KEY", "status": "warn",
         "effect": "no AI", "remediation": "set the env var"},
    ])
    sd._print_config_check()
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "WARN" in out
    assert "GATEWAY_SSO_SECRET" in out
    assert "ANTHROPIC_API_KEY" in out
    assert "→" in out  # remediation arrow rendered
