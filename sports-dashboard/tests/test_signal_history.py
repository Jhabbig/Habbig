"""Tests for the public per-signal ledger (/signal-history)."""
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import sports_dashboard as sd


def _setup_isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sports_edge_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, home_team TEXT, away_team TEXT, outcome TEXT,
            sharp_prob REAL, poly_prob REAL, divergence REAL,
            kelly_pct REAL, confidence_score REAL,
            resolved INTEGER DEFAULT 0, resolution TEXT DEFAULT '',
            detected_at TEXT, commence_time TEXT DEFAULT '',
            event_id TEXT DEFAULT '', market_type TEXT DEFAULT 'h2h'
        );
    """)
    conn.commit()
    conn.close()


def _client():
    return TestClient(sd.app)


def _add(sport="nba", outcome="A", divergence=6.0, resolved=1,
         resolution="correct", offset_days=1, market_type="h2h"):
    ts = (datetime.now(timezone.utc) - timedelta(days=offset_days)).isoformat()
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_edge_history (sport, home_team, away_team, outcome, "
        "sharp_prob, poly_prob, divergence, kelly_pct, resolved, resolution, "
        "detected_at, market_type) "
        "VALUES (?, 'X', 'Y', ?, 55, 49, ?, 1.5, ?, ?, ?, ?)",
        (sport, outcome, divergence, resolved, resolution, ts, market_type),
    )
    conn.commit()
    conn.close()


def test_empty_returns_empty(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    rows = sd._compute_signal_history(None, days=30, limit=100, resolved_only=False)
    assert rows == []


def test_returns_recent_signals(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add(divergence=6.0)
    _add(divergence=7.0)
    rows = sd._compute_signal_history(None, days=30, limit=100, resolved_only=False)
    assert len(rows) == 2


def test_sport_filter(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add(sport="nba")
    _add(sport="nfl")
    rows = sd._compute_signal_history("nba", days=30, limit=100, resolved_only=False)
    assert len(rows) == 1
    assert rows[0]["sport"] == "nba"


def test_resolved_only_filter(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add(resolved=1, resolution="correct")
    _add(resolved=0, resolution="")
    rows_all = sd._compute_signal_history(None, days=30, limit=100, resolved_only=False)
    rows_resolved = sd._compute_signal_history(None, days=30, limit=100, resolved_only=True)
    assert len(rows_all) == 2
    assert len(rows_resolved) == 1


def test_window_filter(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add(offset_days=200)  # outside 30d window
    _add(offset_days=5)
    rows = sd._compute_signal_history(None, days=30, limit=100, resolved_only=False)
    assert len(rows) == 1


def test_limit_caps_results(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    for _ in range(20):
        _add()
    rows = sd._compute_signal_history(None, days=30, limit=5, resolved_only=False)
    assert len(rows) == 5


def test_ordered_newest_first(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add(outcome="old", offset_days=10)
    _add(outcome="newer", offset_days=2)
    _add(outcome="newest", offset_days=1)
    rows = sd._compute_signal_history(None, days=30, limit=100, resolved_only=False)
    assert [r["outcome"] for r in rows] == ["newest", "newer", "old"]


# ── Endpoint ────────────────────────────────────────────────────────────────

def test_endpoint_anonymous_readable(tmp_path, monkeypatch):
    """Same conversion-surface logic as /track-record — public read."""
    _setup_isolated_db(tmp_path, monkeypatch)
    r = _client().get("/api/signal-history")
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body
    assert body["n_total"] == 0


def test_endpoint_aggregates(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add(resolution="correct")
    _add(resolution="correct")
    _add(resolution="incorrect")
    _add(resolved=0, resolution="")
    r = _client().get("/api/signal-history")
    body = r.json()
    assert body["n_total"] == 4
    assert body["n_resolved"] == 3
    assert body["n_correct"] == 2
    assert abs(body["win_rate"] - (2/3)) < 0.01


def test_endpoint_validates_days_and_limit(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    # Out-of-range / non-int silently clamps to defaults
    r = _client().get("/api/signal-history?days=99999&limit=99999")
    assert r.status_code == 200
    r = _client().get("/api/signal-history?days=abc&limit=xyz")
    assert r.status_code == 200


def test_signal_history_page_is_public():
    """The page itself loads without auth."""
    r = _client().get("/signal-history")
    assert r.status_code == 200
    assert "Signal History" in r.text
