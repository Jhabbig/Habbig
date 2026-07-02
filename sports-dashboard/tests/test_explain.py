"""Tests for the AI-explanation endpoint.

The actual Claude API call is mocked — these tests cover the cache-key
hashing, DB cache layer, payload projection, and the request-handling
branches (missing input, cache hit, cache miss, no API key, API error).
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

import sports_dashboard as sd


def _client():
    return TestClient(sd.app)


def _setup_isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sports_signal_explanations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT NOT NULL UNIQUE,
            signal_summary TEXT,
            explanation TEXT NOT NULL,
            model TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


# ── _signal_cache_key ───────────────────────────────────────────────────────

def test_cache_key_stable_for_identical_signals():
    a = {"home_team": "Lakers", "away_team": "Warriors", "outcome": "Lakers",
         "divergence": 5.2, "sport": "basketball_nba"}
    b = {"home_team": "Lakers", "away_team": "Warriors", "outcome": "Lakers",
         "divergence": 5.2, "sport": "basketball_nba"}
    assert sd._signal_cache_key(a) == sd._signal_cache_key(b)


def test_cache_key_rounds_divergence():
    """5.22 and 5.23 should collapse onto the same key (0.1pp rounding)."""
    a = {"home_team": "Lakers", "away_team": "Warriors", "outcome": "Lakers",
         "divergence": 5.22, "sport": "nba"}
    b = {"home_team": "Lakers", "away_team": "Warriors", "outcome": "Lakers",
         "divergence": 5.23, "sport": "nba"}
    assert sd._signal_cache_key(a) == sd._signal_cache_key(b)


def test_cache_key_changes_when_outcome_differs():
    a = {"home_team": "Lakers", "away_team": "Warriors", "outcome": "Lakers",
         "divergence": 5.0, "sport": "nba"}
    b = {"home_team": "Lakers", "away_team": "Warriors", "outcome": "Warriors",
         "divergence": 5.0, "sport": "nba"}
    assert sd._signal_cache_key(a) != sd._signal_cache_key(b)


def test_cache_key_is_case_insensitive():
    a = {"home_team": "Lakers", "away_team": "Warriors", "outcome": "Lakers",
         "divergence": 5.0, "sport": "nba"}
    b = {"home_team": "LAKERS", "away_team": "WARRIORS", "outcome": "lakers",
         "divergence": 5.0, "sport": "NBA"}
    assert sd._signal_cache_key(a) == sd._signal_cache_key(b)


def test_cache_key_accepts_outcome_name_alias():
    """Comparison rows use 'outcome' or 'outcome_name' — both should key the same."""
    a = {"home_team": "X", "away_team": "Y", "outcome": "X", "divergence": 5.0, "sport": "s"}
    b = {"home_team": "X", "away_team": "Y", "outcome_name": "X", "divergence": 5.0, "sport": "s"}
    assert sd._signal_cache_key(a) == sd._signal_cache_key(b)


# ── _build_explain_payload ──────────────────────────────────────────────────

def test_payload_pulls_consensus_devigged_when_present():
    """Player-prop rows use consensus_over_devigged; comparison rows use
    true_prob_no_vig; futures fall back to consensus_prob."""
    p = sd._build_explain_payload({"consensus_over_devigged": 52.0})
    assert p["consensus_devigged_pct"] == 52.0
    p = sd._build_explain_payload({"true_prob_no_vig": 51.0})
    assert p["consensus_devigged_pct"] == 51.0
    p = sd._build_explain_payload({"consensus_prob": 50.0})
    assert p["consensus_devigged_pct"] == 50.0


def test_payload_accepts_divergence_or_divergence_pct():
    p = sd._build_explain_payload({"divergence": 7.0})
    assert p["divergence_pp"] == 7.0
    p = sd._build_explain_payload({"divergence_pct": 8.0})
    assert p["divergence_pp"] == 8.0


# ── DB cache ────────────────────────────────────────────────────────────────

def test_cache_round_trip(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    signal = {"home_team": "A", "away_team": "B", "outcome": "A",
              "divergence": 5.0, "sport": "nba"}
    key = sd._signal_cache_key(signal)
    sd._store_explanation(key, signal, "Polymarket is 5pp behind the sharp consensus.", "claude-opus-4-7")
    got = sd._get_cached_explanation(key)
    assert got == "Polymarket is 5pp behind the sharp consensus."


def test_cache_returns_none_when_expired(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "EXPLAIN_CACHE_TTL_SECONDS", 1800)
    key = "expired-key"
    # Insert with a created_at well outside the window. Match SQLite's
    # datetime('now') format so lexicographic comparison works correctly.
    expired_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_signal_explanations (cache_key, explanation, created_at) "
        "VALUES (?, ?, ?)",
        (key, "stale", expired_ts),
    )
    conn.commit()
    conn.close()
    assert sd._get_cached_explanation(key) is None


def test_store_upserts_on_existing_key(tmp_path, monkeypatch):
    """A second store with the same cache_key should overwrite, not duplicate."""
    _setup_isolated_db(tmp_path, monkeypatch)
    signal = {"home_team": "A", "away_team": "B", "outcome": "A",
              "divergence": 5.0, "sport": "nba"}
    key = sd._signal_cache_key(signal)
    sd._store_explanation(key, signal, "first", "claude-opus-4-7")
    sd._store_explanation(key, signal, "second", "claude-opus-4-7")
    with sd._get_db() as conn:
        rows = conn.execute(
            "SELECT explanation FROM sports_signal_explanations WHERE cache_key = ?",
            (key,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["explanation"] == "second"


# ── /api/signals/explain endpoint ───────────────────────────────────────────

def test_explain_returns_400_when_signal_missing_identity():
    r = _client().post("/api/signals/explain", json={"divergence": 5.0})
    assert r.status_code == 400


def test_explain_returns_400_when_body_is_not_object():
    r = _client().post("/api/signals/explain", json=[1, 2, 3])
    assert r.status_code == 400


def test_explain_returns_cached_when_available(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    signal = {"home_team": "Lakers", "away_team": "Warriors", "outcome": "Lakers",
              "divergence": 5.2, "sport": "basketball_nba"}
    key = sd._signal_cache_key(signal)
    sd._store_explanation(key, signal, "Polymarket is 5pp behind.", "claude-opus-4-7")

    r = _client().post("/api/signals/explain", json=signal)
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is True
    assert "Polymarket is 5pp behind" in body["explanation"]


def test_explain_returns_503_when_no_api_key(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "ANTHROPIC_API_KEY", "")
    signal = {"home_team": "X", "away_team": "Y", "outcome": "X",
              "divergence": 6.0, "sport": "nba"}
    r = _client().post("/api/signals/explain", json=signal)
    assert r.status_code == 503


def test_explain_writes_to_cache_after_api_call(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "ANTHROPIC_API_KEY", "test-key")

    # Patch the Claude call to avoid network. We patch the wrapper rather
    # than the SDK so we don't need to construct fake content blocks.
    fake = "Polymarket lagged Pinnacle by 6pp; expect a steam move within the hour."

    def fake_call(signal):
        return fake

    monkeypatch.setattr(sd, "_explain_signal_via_claude", fake_call)

    signal = {"home_team": "Lakers", "away_team": "Warriors", "outcome": "Lakers",
              "divergence": 6.0, "sport": "basketball_nba"}
    r = _client().post("/api/signals/explain", json=signal)
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is False
    assert body["explanation"] == fake

    # Hit again — should be cached now
    r2 = _client().post("/api/signals/explain", json=signal)
    assert r2.status_code == 200
    assert r2.json()["cached"] is True


def test_explain_returns_502_when_model_returns_empty(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(sd, "_explain_signal_via_claude", lambda s: "")
    signal = {"home_team": "X", "away_team": "Y", "outcome": "X",
              "divergence": 6.0, "sport": "nba"}
    r = _client().post("/api/signals/explain", json=signal)
    assert r.status_code == 502


def test_explain_returns_502_when_model_raises(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sd, "ANTHROPIC_API_KEY", "test-key")

    def boom(s):
        raise RuntimeError("API quota exhausted")

    monkeypatch.setattr(sd, "_explain_signal_via_claude", boom)
    signal = {"home_team": "X", "away_team": "Y", "outcome": "X",
              "divergence": 6.0, "sport": "nba"}
    r = _client().post("/api/signals/explain", json=signal)
    assert r.status_code == 502
    assert "API quota exhausted" in r.json()["error"]
