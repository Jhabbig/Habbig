"""Tests for the steam-move detector + closing-line consensus helpers."""
import sqlite3
from datetime import datetime, timedelta, timezone

import sports_dashboard as sd


def _setup_isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(sd, "_DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sports_market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, event_name TEXT, outcome TEXT,
            book_prob REAL, poly_prob REAL, kalshi_prob REAL,
            divergence REAL, poly_volume REAL, kalshi_volume REAL,
            snapshot_at TEXT, market_type TEXT DEFAULT 'h2h'
        );
        CREATE TABLE sports_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, event_id TEXT,
            home_team TEXT, away_team TEXT,
            home_score INTEGER, away_score INTEGER,
            completed INTEGER DEFAULT 0,
            winner TEXT DEFAULT '',
            commence_time TEXT,
            fetched_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


def _snap(sport, event, outcome, prob, ts_offset_min,
          poly_prob=None, kalshi_prob=None):
    """Insert a snapshot at NOW - ts_offset_min minutes."""
    snap_at = (datetime.now(timezone.utc)
               - timedelta(minutes=ts_offset_min)).isoformat()
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_market_snapshots "
        "(sport, event_name, outcome, book_prob, poly_prob, kalshi_prob, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sport, event, outcome, prob, poly_prob, kalshi_prob, snap_at),
    )
    conn.commit()
    conn.close()


# ── _detect_steam_moves ─────────────────────────────────────────────────────

def test_empty_history(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    assert sd._detect_steam_moves(None) == []


def test_detects_simple_steam_move(tmp_path, monkeypatch):
    """Two snapshots 10 min apart with +3pp swing should fire."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "A vs B", "A", 50.0, ts_offset_min=20)
    _snap("nba", "A vs B", "A", 53.0, ts_offset_min=10)
    moves = sd._detect_steam_moves("nba", hours=24)
    assert len(moves) == 1
    m = moves[0]
    assert m["delta_pp"] == 3.0
    assert m["from_prob"] == 50.0
    assert m["to_prob"] == 53.0
    assert 9.5 < m["elapsed_min"] < 10.5


def test_swing_below_threshold_ignored(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "A vs B", "A", 50.0, ts_offset_min=20)
    _snap("nba", "A vs B", "A", 51.5, ts_offset_min=10)  # only 1.5pp swing
    assert sd._detect_steam_moves("nba", hours=24) == []


def test_swing_outside_window_ignored(tmp_path, monkeypatch):
    """Window is 30 min by default — a swing 60 min apart should not fire."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "A vs B", "A", 50.0, ts_offset_min=70)
    _snap("nba", "A vs B", "A", 55.0, ts_offset_min=10)
    assert sd._detect_steam_moves("nba", hours=24) == []


def test_detects_within_window(tmp_path, monkeypatch):
    """Same data but with a 90-min window should fire."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "A vs B", "A", 50.0, ts_offset_min=70)
    _snap("nba", "A vs B", "A", 55.0, ts_offset_min=10)
    moves = sd._detect_steam_moves("nba", hours=24, window_min=90)
    assert len(moves) == 1
    assert moves[0]["delta_pp"] == 5.0


def test_negative_delta_preserved(tmp_path, monkeypatch):
    """A drop in book_prob is still a steam move (the other side firmed up)."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "A vs B", "A", 55.0, ts_offset_min=20)
    _snap("nba", "A vs B", "A", 50.0, ts_offset_min=10)
    moves = sd._detect_steam_moves("nba", hours=24)
    assert len(moves) == 1
    assert moves[0]["delta_pp"] == -5.0


def test_sport_filter(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "A vs B", "A", 50.0, 20)
    _snap("nba", "A vs B", "A", 55.0, 10)
    _snap("nfl", "X vs Y", "X", 50.0, 20)
    _snap("nfl", "X vs Y", "X", 55.0, 10)
    moves = sd._detect_steam_moves("nba", hours=24)
    assert len(moves) == 1
    assert moves[0]["sport"] == "nba"


def test_per_event_outcome_isolation(tmp_path, monkeypatch):
    """A steam move on outcome A must not be reported on outcome B."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "A vs B", "A", 50.0, 20)
    _snap("nba", "A vs B", "A", 55.0, 10)
    _snap("nba", "A vs B", "B", 50.0, 20)
    _snap("nba", "A vs B", "B", 50.5, 10)  # only 0.5pp on B
    moves = sd._detect_steam_moves("nba", hours=24)
    assert len(moves) == 1
    assert moves[0]["outcome"] == "A"


def test_sort_by_abs_delta_desc(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    # Big move
    _snap("nba", "Big vs B", "Big", 50.0, 20)
    _snap("nba", "Big vs B", "Big", 60.0, 10)
    # Small move
    _snap("nba", "Small vs B", "Small", 50.0, 20)
    _snap("nba", "Small vs B", "Small", 52.5, 10)
    moves = sd._detect_steam_moves("nba", hours=24)
    assert abs(moves[0]["delta_pp"]) > abs(moves[1]["delta_pp"])


def test_min_delta_pp_parameter(tmp_path, monkeypatch):
    """Raising the min threshold should drop borderline swings."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "A vs B", "A", 50.0, 20)
    _snap("nba", "A vs B", "A", 52.5, 10)
    assert sd._detect_steam_moves("nba", hours=24, min_delta_pp=5.0) == []
    moves = sd._detect_steam_moves("nba", hours=24, min_delta_pp=2.0)
    assert len(moves) == 1


def test_passes_through_poly_kalshi(tmp_path, monkeypatch):
    """The poly/kalshi prob at the time of the late snapshot should be
    surfaced so the UI can show 'sharps moved; Poly hasn't yet'."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "A vs B", "A", 50.0, 20, poly_prob=48.0, kalshi_prob=49.0)
    _snap("nba", "A vs B", "A", 55.0, 10, poly_prob=48.5, kalshi_prob=49.5)
    m = sd._detect_steam_moves("nba", hours=24)[0]
    assert m["poly_prob"] == 48.5
    assert m["kalshi_prob"] == 49.5


# ── _compute_closing_lines ──────────────────────────────────────────────────

def _add_score(sport, home, away, commence_offset_hours):
    commence = (datetime.now(timezone.utc)
                + timedelta(hours=commence_offset_hours)).isoformat()
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_scores (sport, event_id, home_team, away_team, commence_time) "
        "VALUES (?, ?, ?, ?, ?)",
        (sport, f"e_{home}_{away}", home, away, commence),
    )
    conn.commit()
    conn.close()


def test_closing_lines_empty(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    assert sd._compute_closing_lines(None) == []


def test_closing_line_picks_latest_before_commence(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_score("nba", "Lakers", "Warriors", commence_offset_hours=-1)  # 1h ago
    # Snapshots from 5h ago, 2h ago, and 30min AFTER kickoff
    _snap("nba", "Lakers vs Warriors", "Lakers", 50.0, 300)
    _snap("nba", "Lakers vs Warriors", "Lakers", 53.0, 120)
    # Snapshot after kickoff (offset_min < 60 since kickoff was 60 min ago)
    _snap("nba", "Lakers vs Warriors", "Lakers", 55.0, 30)
    rows = sd._compute_closing_lines("nba", days=7)
    assert len(rows) == 1
    # 30-min-ago snapshot is AFTER kickoff, so it should not be the closing line.
    # 2-hr-ago snapshot is before kickoff -> that's the closing line.
    assert rows[0]["closing_book_prob"] == 53.0


def test_closing_line_falls_back_to_latest_when_no_score(tmp_path, monkeypatch):
    """If no score row matches, we take the latest snapshot in the window."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "Foo vs Bar", "Foo", 50.0, 300)
    _snap("nba", "Foo vs Bar", "Foo", 55.0, 60)  # latest
    rows = sd._compute_closing_lines("nba", days=7)
    assert len(rows) == 1
    assert rows[0]["closing_book_prob"] == 55.0


def test_closing_lines_filtered_by_sport(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _snap("nba", "A vs B", "A", 50.0, 60)
    _snap("nfl", "X vs Y", "X", 60.0, 60)
    rows = sd._compute_closing_lines("nba", days=7)
    assert len(rows) == 1
    assert rows[0]["sport"] == "nba"
