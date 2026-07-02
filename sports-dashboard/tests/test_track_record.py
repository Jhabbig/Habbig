"""Tests for the track-record helpers (CLV, P&L sim, calibration).

These run against a fresh in-memory copy of the production schema, so
each test sets up its own fixture data without touching a live DB.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import sports_dashboard as sd


def _setup_isolated_db(tmp_path, monkeypatch):
    """Point sports_dashboard at a fresh sqlite file with schema applied."""
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
        CREATE TABLE sports_market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, event_name TEXT, outcome TEXT,
            book_prob REAL, poly_prob REAL, kalshi_prob REAL,
            divergence REAL, poly_volume REAL, kalshi_volume REAL,
            snapshot_at TEXT, market_type TEXT DEFAULT 'h2h'
        );
    """)
    conn.commit()
    conn.close()


# ── CLV ─────────────────────────────────────────────────────────────────────

def test_clv_returns_zero_summary_on_empty(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    out = sd._compute_clv(days=30)
    assert out["overall"]["n"] == 0


def test_clv_positive_when_market_moves_toward_prediction(tmp_path, monkeypatch):
    """We bet YES at poly=35; line closes at poly=40; +5pp CLV in our direction."""
    _setup_isolated_db(tmp_path, monkeypatch)
    detected = datetime.now(timezone.utc) - timedelta(hours=2)
    commence = datetime.now(timezone.utc) - timedelta(minutes=5)
    snapshot_at = detected + timedelta(minutes=30)

    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_edge_history (sport, home_team, away_team, outcome, sharp_prob, poly_prob, divergence, resolved, resolution, detected_at, commence_time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'correct', ?, ?)",
        ("nba", "Lakers", "Warriors", "Lakers", 41.0, 35.0, 6.0,
         detected.isoformat(), commence.isoformat()),
    )
    conn.execute(
        "INSERT INTO sports_market_snapshots (sport, event_name, outcome, book_prob, poly_prob, divergence, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("nba", "Lakers vs Warriors", "Lakers", 42.0, 40.0, 2.0, snapshot_at.isoformat()),
    )
    conn.commit()
    conn.close()

    out = sd._compute_clv(days=30)
    assert out["overall"]["n"] == 1
    # poly 35 -> 40, we bet YES (divergence > 0): CLV = +5
    assert out["overall"]["mean"] == 5.0
    assert out["overall"]["positive_rate"] == 1.0


def test_clv_negative_when_market_moves_against_prediction(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    detected = datetime.now(timezone.utc) - timedelta(hours=2)
    commence = datetime.now(timezone.utc) - timedelta(minutes=5)

    conn = sqlite3.connect(str(sd._DB_PATH))
    # We bet YES at 40; closing line drifts to 35.
    conn.execute(
        "INSERT INTO sports_edge_history (sport, home_team, away_team, outcome, sharp_prob, poly_prob, divergence, resolved, resolution, detected_at, commence_time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'incorrect', ?, ?)",
        ("nba", "Lakers", "Warriors", "Lakers", 46.0, 40.0, 6.0,
         detected.isoformat(), commence.isoformat()),
    )
    conn.execute(
        "INSERT INTO sports_market_snapshots (sport, event_name, outcome, poly_prob, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("nba", "Lakers vs Warriors", "Lakers", 35.0,
         (detected + timedelta(minutes=10)).isoformat()),
    )
    conn.commit()
    conn.close()

    out = sd._compute_clv(days=30)
    assert out["overall"]["mean"] == -5.0


# ── P&L simulation ──────────────────────────────────────────────────────────

def test_pnl_zero_on_empty(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    out = sd._compute_pnl_simulation(days=30, threshold_pp=5, stake=100)
    assert out["n_bets"] == 0
    assert out["total_pnl"] == 0.0


def test_pnl_one_winning_bet(tmp_path, monkeypatch):
    """One winning bet at poly=50 means stake doubles. $100 stake -> $100 profit."""
    _setup_isolated_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_edge_history (sport, divergence, poly_prob, resolved, resolution, detected_at) "
        "VALUES ('nba', 6.0, 50.0, 1, 'correct', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()
    out = sd._compute_pnl_simulation(days=30, threshold_pp=5, stake=100)
    assert out["n_bets"] == 1
    # Win at poly=50: profit = 100 * (100/50 - 1) = $100
    assert out["total_pnl"] == 100.0
    assert out["win_rate"] == 1.0


def test_pnl_one_losing_bet(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_edge_history (sport, divergence, poly_prob, resolved, resolution, detected_at) "
        "VALUES ('nba', 6.0, 50.0, 1, 'incorrect', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()
    out = sd._compute_pnl_simulation(days=30, threshold_pp=5, stake=100)
    assert out["total_pnl"] == -100.0
    assert out["win_rate"] == 0.0


def test_pnl_threshold_filters(tmp_path, monkeypatch):
    """A 3pp signal is below the 5pp threshold and should be excluded."""
    _setup_isolated_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_edge_history (sport, divergence, poly_prob, resolved, resolution, detected_at) "
        "VALUES ('nba', 3.0, 50.0, 1, 'correct', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()
    out = sd._compute_pnl_simulation(days=30, threshold_pp=5, stake=100)
    assert out["n_bets"] == 0


# ── Calibration ─────────────────────────────────────────────────────────────

def test_calibration_empty_returns_all_bins(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    out = sd._compute_calibration(days=30)
    assert len(out["bins"]) == 5
    assert all(b["n"] == 0 for b in out["bins"])


def test_calibration_buckets_signals_correctly(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(sd._DB_PATH))
    now = datetime.now(timezone.utc).isoformat()
    # 3 signals: 6pp (bin [5,10)), 12pp (bin [10,15)), 25pp (bin [20,inf))
    for div, sharp, res in [(6.0, 55.0, "correct"), (12.0, 60.0, "incorrect"),
                             (25.0, 70.0, "correct")]:
        conn.execute(
            "INSERT INTO sports_edge_history (sport, divergence, sharp_prob, resolved, resolution, detected_at) "
            "VALUES ('nba', ?, ?, 1, ?, ?)",
            (div, sharp, res, now),
        )
    conn.commit()
    conn.close()

    out = sd._compute_calibration(days=30)
    bins_by_lo = {b["lo"]: b for b in out["bins"]}
    assert bins_by_lo[5]["n"] == 1
    assert bins_by_lo[5]["win_rate"] == 1.0
    assert bins_by_lo[10]["n"] == 1
    assert bins_by_lo[10]["win_rate"] == 0.0
    assert bins_by_lo[20]["n"] == 1
    assert bins_by_lo[20]["win_rate"] == 1.0
