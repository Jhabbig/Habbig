"""Tests for the bet-tracker enrichment: CLV computation + per-group stats."""
import sqlite3
from datetime import datetime, timedelta, timezone

import sports_dashboard as sd


def _setup_isolated_db(tmp_path, monkeypatch):
    """Point sd at a fresh sqlite with the trade + snapshot schema."""
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sports_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, market_name TEXT, outcome TEXT,
            entry_price REAL, amount REAL,
            exit_price REAL, pnl REAL,
            status TEXT DEFAULT 'open',
            resolved_at TEXT, created_at TEXT DEFAULT (datetime('now')),
            sport TEXT, book TEXT, market_type TEXT,
            line REAL, commence_time TEXT, source TEXT,
            closing_book_prob REAL, clv_pp REAL,
            notes TEXT, home_team TEXT, away_team TEXT
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


# ── _compute_trade_clv ──────────────────────────────────────────────────────

def test_clv_returns_none_when_no_snapshot(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    trade = {
        "sport": "nba", "home_team": "Lakers", "away_team": "Warriors",
        "outcome": "Lakers",
        "entry_price": 40.0,
        "commence_time": datetime.now(timezone.utc).isoformat(),
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    }
    closing, clv = sd._compute_trade_clv(trade)
    assert closing is None
    assert clv is None


def test_clv_positive_when_line_moves_toward_bettor(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    created = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    commence = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshot_at = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_market_snapshots "
        "(sport, event_name, outcome, poly_prob, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("nba", "Lakers vs Warriors", "Lakers", 47.0, snapshot_at),
    )
    conn.commit()
    conn.close()
    trade = {
        "sport": "nba", "home_team": "Lakers", "away_team": "Warriors",
        "outcome": "Lakers", "entry_price": 40.0,
        "commence_time": commence, "created_at": created,
    }
    closing, clv = sd._compute_trade_clv(trade)
    assert closing == 47.0
    assert clv == 7.0  # bet at 40, closed at 47 → +7 pp CLV


def test_clv_negative_when_line_moves_against_bettor(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    created = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    commence = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshot_at = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_market_snapshots "
        "(sport, event_name, outcome, poly_prob, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("nba", "Lakers vs Warriors", "Lakers", 35.0, snapshot_at),
    )
    conn.commit()
    conn.close()
    trade = {
        "sport": "nba", "home_team": "Lakers", "away_team": "Warriors",
        "outcome": "Lakers", "entry_price": 40.0,
        "commence_time": commence, "created_at": created,
    }
    closing, clv = sd._compute_trade_clv(trade)
    assert closing == 35.0
    assert clv == -5.0


def test_clv_picks_latest_snapshot_before_commence(tmp_path, monkeypatch):
    """Multiple snapshots — we want the one closest to (but before) kickoff."""
    _setup_isolated_db(tmp_path, monkeypatch)
    created = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    commence = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    early = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    late = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    conn = sqlite3.connect(str(sd._DB_PATH))
    for ts, prob in [(early, 42.0), (late, 47.0)]:
        conn.execute(
            "INSERT INTO sports_market_snapshots "
            "(sport, event_name, outcome, poly_prob, snapshot_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("nba", "Lakers vs Warriors", "Lakers", prob, ts),
        )
    conn.commit()
    conn.close()
    trade = {
        "sport": "nba", "home_team": "Lakers", "away_team": "Warriors",
        "outcome": "Lakers", "entry_price": 40.0,
        "commence_time": commence, "created_at": created,
    }
    closing, clv = sd._compute_trade_clv(trade)
    assert closing == 47.0  # the later snapshot wins


def test_clv_requires_event_outcome_commence(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    assert sd._compute_trade_clv({"outcome": "X"}) == (None, None)
    assert sd._compute_trade_clv({"home_team": "X"}) == (None, None)


# ── _trade_stats_summary ────────────────────────────────────────────────────

def test_stats_empty():
    s = sd._trade_stats_summary([])
    assert s["n_closed"] == 0
    assert s["n_open"] == 0
    assert s["total_pnl"] == 0
    assert s["win_rate"] == 0.0
    assert s["mean_clv_pp"] is None


def test_stats_pnl_winrate_roi():
    trades = [
        {"status": "closed", "pnl": 50, "amount": 100, "clv_pp": 3.5},
        {"status": "closed", "pnl": -100, "amount": 100, "clv_pp": -2.0},
        {"status": "closed", "pnl": 200, "amount": 100, "clv_pp": 6.0},
        {"status": "open", "amount": 100},
    ]
    s = sd._trade_stats_summary(trades)
    assert s["n_closed"] == 3
    assert s["n_open"] == 1
    assert s["total_pnl"] == 150
    assert s["win_rate"] == round(2/3, 4)  # 2 wins of 3 closed
    assert s["roi_pct"] == 50.0           # $150 / $300 staked
    # Mean CLV = (3.5 - 2.0 + 6.0) / 3 ≈ 2.5
    assert s["mean_clv_pp"] == round(7.5/3, 3)
    assert s["n_with_clv"] == 3


def test_stats_ignores_missing_clv():
    """Trades without a clv_pp should not be counted in the CLV mean."""
    trades = [
        {"status": "closed", "pnl": 10, "amount": 100, "clv_pp": 4.0},
        {"status": "closed", "pnl": 20, "amount": 100},  # no clv_pp
        {"status": "closed", "pnl": -10, "amount": 100, "clv_pp": -1.0},
    ]
    s = sd._trade_stats_summary(trades)
    assert s["n_with_clv"] == 2
    assert s["mean_clv_pp"] == 1.5  # (4 + -1) / 2


def test_stats_handles_only_open_trades():
    """An all-open portfolio has zeroed P&L/win/roi but n_open > 0."""
    s = sd._trade_stats_summary([
        {"status": "open", "amount": 100},
        {"status": "open", "amount": 50},
    ])
    assert s["n_open"] == 2
    assert s["n_closed"] == 0
    assert s["total_pnl"] == 0
    assert s["roi_pct"] == 0.0
