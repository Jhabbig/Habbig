"""Tests for the bankroll + Kelly stake suggester (T4.3)."""
import sqlite3

from fastapi.testclient import TestClient

import sports_dashboard as sd


def _setup_isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sports_bankroll (
            user_id TEXT PRIMARY KEY,
            starting_bankroll REAL NOT NULL,
            current_bankroll REAL NOT NULL,
            kelly_fraction REAL DEFAULT 0.5,
            max_per_bet_pct REAL DEFAULT 5.0,
            drawdown_alert_pct REAL DEFAULT 10.0,
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


def _client():
    return TestClient(sd.app)


# ── _kelly_suggested_stake ──────────────────────────────────────────────────

def test_no_bankroll_returns_zero():
    s = sd._kelly_suggested_stake({"current_bankroll": 0}, 10.0)
    assert s["stake_usd"] == 0.0
    assert s["capped_by"] == "no_edge"


def test_no_edge_returns_zero():
    s = sd._kelly_suggested_stake({"current_bankroll": 10000}, 0.0)
    assert s["stake_usd"] == 0.0
    assert s["capped_by"] == "no_edge"


def test_kelly_half_default_scales_correctly():
    """match_and_compare already applies half-Kelly, so a 4.0 input
    represents 8% full Kelly. With kelly_fraction=0.5 (half-Kelly), we
    round-trip back to 4.0% of bankroll. On $10k that's $400."""
    s = sd._kelly_suggested_stake(
        {"current_bankroll": 10000, "kelly_fraction": 0.5, "max_per_bet_pct": 10.0},
        kelly_pct=4.0,
    )
    assert abs(s["stake_usd"] - 400.0) < 0.01
    assert s["capped_by"] == "kelly"


def test_kelly_fraction_can_be_quarter():
    """With kelly_fraction=0.25, the same 4.0 input -> 2% of bankroll."""
    s = sd._kelly_suggested_stake(
        {"current_bankroll": 10000, "kelly_fraction": 0.25, "max_per_bet_pct": 10.0},
        kelly_pct=4.0,
    )
    assert abs(s["stake_usd"] - 200.0) < 0.01


def test_kelly_fraction_can_be_full():
    s = sd._kelly_suggested_stake(
        {"current_bankroll": 10000, "kelly_fraction": 1.0, "max_per_bet_pct": 20.0},
        kelly_pct=4.0,
    )
    assert abs(s["stake_usd"] - 800.0) < 0.01


def test_max_per_bet_pct_caps_high_kelly():
    """A very strong edge (10% half-Kelly -> 20% full -> 10% at half-K)
    should get capped at the user's 5% max-per-bet ceiling."""
    s = sd._kelly_suggested_stake(
        {"current_bankroll": 10000, "kelly_fraction": 0.5, "max_per_bet_pct": 5.0},
        kelly_pct=10.0,
    )
    assert s["stake_usd"] == 500.0  # 5% of $10k
    assert s["capped_by"] == "max_per_bet_pct"


def test_negative_kelly_treated_as_no_edge():
    s = sd._kelly_suggested_stake({"current_bankroll": 10000}, -1.0)
    assert s["stake_usd"] == 0.0
    assert s["capped_by"] == "no_edge"


# ── _annotate_bankroll ──────────────────────────────────────────────────────

def test_annotate_returns_none_when_unset():
    assert sd._annotate_bankroll(None) is None


def test_annotate_computes_pnl_and_return_pct():
    out = sd._annotate_bankroll({
        "starting_bankroll": 10000,
        "current_bankroll": 11500,
        "drawdown_alert_pct": 10.0,
    })
    assert out["pnl"] == 1500.0
    assert out["return_pct"] == 15.0
    assert out["in_drawdown"] is False


def test_annotate_drawdown_flag():
    """Down >10% from starting -> in_drawdown True."""
    out = sd._annotate_bankroll({
        "starting_bankroll": 10000,
        "current_bankroll": 8500,  # -15%
        "drawdown_alert_pct": 10.0,
    })
    assert out["in_drawdown"] is True
    assert out["return_pct"] == -15.0


def test_annotate_drawdown_threshold_configurable():
    """User wants the alert to fire only at -20%. -15% should NOT trip it."""
    out = sd._annotate_bankroll({
        "starting_bankroll": 10000,
        "current_bankroll": 8500,
        "drawdown_alert_pct": 20.0,
    })
    assert out["in_drawdown"] is False


# ── /api/bankroll endpoints ─────────────────────────────────────────────────

def test_get_bankroll_returns_null_when_unset(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().get("/api/bankroll")
    assert r.status_code == 200
    assert r.json()["bankroll"] is None


def test_put_bankroll_creates(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().put("/api/bankroll", json={"starting_bankroll": 10000})
    assert r.status_code == 200
    body = r.json()["bankroll"]
    assert body["starting_bankroll"] == 10000
    assert body["current_bankroll"] == 10000  # defaults to starting
    assert body["kelly_fraction"] == 0.5      # default
    assert body["max_per_bet_pct"] == 5.0     # default


def test_put_bankroll_updates_in_place(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    c = _client()
    c.put("/api/bankroll", json={"starting_bankroll": 10000})
    c.put("/api/bankroll", json={
        "starting_bankroll": 10000,
        "current_bankroll": 12000,
        "kelly_fraction": 0.25,
        "max_per_bet_pct": 3.0,
    })
    body = c.get("/api/bankroll").json()["bankroll"]
    assert body["current_bankroll"] == 12000
    assert body["kelly_fraction"] == 0.25
    assert body["max_per_bet_pct"] == 3.0
    assert body["pnl"] == 2000
    assert body["return_pct"] == 20.0


def test_put_bankroll_rejects_invalid_starting(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().put("/api/bankroll", json={"starting_bankroll": 0})
    assert r.status_code == 400
    r = _client().put("/api/bankroll", json={"starting_bankroll": -100})
    assert r.status_code == 400


def test_put_bankroll_rejects_out_of_range_kelly(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().put("/api/bankroll", json={
        "starting_bankroll": 10000, "kelly_fraction": 1.5
    })
    assert r.status_code == 400


def test_put_bankroll_rejects_out_of_range_max_per_bet(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().put("/api/bankroll", json={
        "starting_bankroll": 10000, "max_per_bet_pct": 200
    })
    assert r.status_code == 400


def test_put_bankroll_clamps_drawdown_alert_pct(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().put("/api/bankroll", json={
        "starting_bankroll": 10000, "drawdown_alert_pct": 0
    })
    assert r.status_code == 400


# ── /api/bankroll/suggest-stake ─────────────────────────────────────────────

def test_suggest_stake_returns_404_when_no_bankroll(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().post("/api/bankroll/suggest-stake", json={"kelly_pct": 3.0})
    assert r.status_code == 404


def test_suggest_stake_returns_suggestion(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    c = _client()
    c.put("/api/bankroll", json={
        "starting_bankroll": 10000, "kelly_fraction": 0.5, "max_per_bet_pct": 10.0
    })
    r = c.post("/api/bankroll/suggest-stake", json={"kelly_pct": 4.0})
    assert r.status_code == 200
    body = r.json()
    assert "suggestion" in body
    assert abs(body["suggestion"]["stake_usd"] - 400.0) < 0.01
    assert body["suggestion"]["capped_by"] == "kelly"
    assert body["in_drawdown"] is False


def test_suggest_stake_surfaces_drawdown(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    c = _client()
    c.put("/api/bankroll", json={
        "starting_bankroll": 10000, "current_bankroll": 8500,
        "kelly_fraction": 0.5, "max_per_bet_pct": 5.0,
        "drawdown_alert_pct": 10.0,
    })
    r = c.post("/api/bankroll/suggest-stake", json={"kelly_pct": 3.0})
    assert r.status_code == 200
    assert r.json()["in_drawdown"] is True


def test_suggest_stake_validates_kelly_pct_type(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _client().put("/api/bankroll", json={"starting_bankroll": 10000})
    r = _client().post("/api/bankroll/suggest-stake", json={"kelly_pct": "huge"})
    assert r.status_code == 400
