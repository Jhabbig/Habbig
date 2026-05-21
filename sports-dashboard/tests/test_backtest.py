"""Tests for the backtest-replay simulator (T4.4)."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

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


def _add_signal(sport="basketball_nba", home="Lakers", away="Warriors",
                outcome="Lakers", divergence=8.0, poly_prob=50.0,
                resolution="correct", market_type="h2h",
                detected_offset_days=1, commence_offset_hours=2):
    """Insert a resolved signal `detected_offset_days` days ago that
    kicks off `commence_offset_hours` after its detection."""
    detected = datetime.now(timezone.utc) - timedelta(days=detected_offset_days)
    commence = detected + timedelta(hours=commence_offset_hours)
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_edge_history "
        "(sport, home_team, away_team, outcome, sharp_prob, poly_prob, "
        "divergence, resolved, resolution, detected_at, commence_time, market_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (sport, home, away, outcome, poly_prob + divergence, poly_prob,
         divergence, resolution, detected.isoformat(),
         commence.isoformat(), market_type),
    )
    conn.commit()
    conn.close()


def _rule(**overrides):
    """Build a rule dict in the on-disk shape (JSON strings for list fields)."""
    base = {
        "sports": "[]",
        "market_types": "[]",
        "min_divergence_pp": 5.0,
        "min_volume": None,
        "max_time_to_event_hours": None,
        "require_sharp_consensus": 1,
        "require_not_stale": 1,
        "require_liquidity_ok": 1,
    }
    for k, v in overrides.items():
        if k in ("sports", "market_types") and isinstance(v, list):
            base[k] = json.dumps(v)
        else:
            base[k] = v
    return base


# ── _signal_from_edge_row ───────────────────────────────────────────────────

def test_reshape_pulls_event_fields():
    row = {
        "sport": "nba",
        "home_team": "Lakers", "away_team": "Warriors", "outcome": "Lakers",
        "divergence": 7.5, "market_type": "h2h",
        "detected_at": "2026-01-15T18:00:00Z",
        "commence_time": "2026-01-15T20:00:00Z",
    }
    sport, signal = sd._signal_from_edge_row(row)
    assert sport == "nba"
    assert signal["home_team"] == "Lakers"
    assert signal["market_type"] == "h2h"
    assert signal["max_divergence"] == 7.5
    assert len(signal["outcomes"]) == 1
    assert signal["outcomes"][0]["divergence_pct"] == 7.5
    assert signal["outcomes"][0]["is_signal"] is True


def test_reshape_computes_time_to_event_hours():
    row = {
        "sport": "nba", "divergence": 5.0,
        "detected_at": "2026-01-15T18:00:00Z",
        "commence_time": "2026-01-15T20:30:00Z",  # 2.5 hours later
    }
    _, signal = sd._signal_from_edge_row(row)
    assert abs(signal["time_to_event_hours"] - 2.5) < 0.01


def test_reshape_handles_missing_timestamps():
    """Old rows may not have commence_time — time_to_event should be None,
    so a rule with max_time_to_event_hours rejects them (not silently
    treating them as time=0)."""
    row = {"sport": "nba", "divergence": 5.0,
           "detected_at": "", "commence_time": ""}
    _, signal = sd._signal_from_edge_row(row)
    assert signal["time_to_event_hours"] is None


# ── _simulate_alert_rule ────────────────────────────────────────────────────

def test_empty_history(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    result = sd._simulate_alert_rule(_rule(), days=30, stake=100)
    assert result["n_bets"] == 0
    assert result["total_pnl"] == 0.0
    assert result["matches"] == []


def test_one_winning_bet(tmp_path, monkeypatch):
    """50% prob -> 2x payout -> $100 stake -> $100 profit on a win."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_signal(divergence=8.0, poly_prob=50.0, resolution="correct")
    r = sd._simulate_alert_rule(_rule(), days=30, stake=100)
    assert r["n_bets"] == 1
    assert r["total_pnl"] == 100.0
    assert r["win_rate"] == 1.0
    assert len(r["matches"]) == 1
    assert r["matches"][0]["resolution"] == "correct"


def test_one_losing_bet(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_signal(divergence=8.0, poly_prob=50.0, resolution="incorrect")
    r = sd._simulate_alert_rule(_rule(), days=30, stake=100)
    assert r["total_pnl"] == -100.0
    assert r["win_rate"] == 0.0


def test_sport_filter_rejects_other_sports(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_signal(sport="basketball_nba", divergence=8.0, resolution="correct")
    _add_signal(sport="soccer_epl", divergence=8.0, resolution="correct")
    r = sd._simulate_alert_rule(_rule(sports=["basketball_nba"]), days=30, stake=100)
    assert r["n_bets"] == 1
    assert r["matches"][0]["sport"] == "basketball_nba"


def test_market_type_filter(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_signal(market_type="h2h", divergence=8.0, resolution="correct")
    _add_signal(market_type="spreads", divergence=8.0, resolution="correct")
    _add_signal(market_type="totals", divergence=8.0, resolution="correct")
    r = sd._simulate_alert_rule(_rule(market_types=["spreads", "totals"]), days=30, stake=100)
    assert r["n_bets"] == 2


def test_min_divergence_filter(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_signal(divergence=3.0, resolution="correct")   # below threshold
    _add_signal(divergence=8.0, resolution="correct")
    r = sd._simulate_alert_rule(_rule(min_divergence_pp=5.0), days=30, stake=100)
    assert r["n_bets"] == 1


def test_max_time_to_event_filter(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    # Both detected 1 day ago, but one kicks off 1h later, the other 10h
    _add_signal(divergence=8.0, resolution="correct", commence_offset_hours=1)
    _add_signal(divergence=8.0, resolution="correct", commence_offset_hours=10)
    r = sd._simulate_alert_rule(_rule(max_time_to_event_hours=4.0), days=30, stake=100)
    assert r["n_bets"] == 1


def test_skips_unresolved_signals(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    # Manually insert an unresolved row to ensure it's filtered out
    conn = sqlite3.connect(str(sd._DB_PATH))
    conn.execute(
        "INSERT INTO sports_edge_history "
        "(sport, home_team, away_team, outcome, sharp_prob, poly_prob, "
        "divergence, resolved, resolution, detected_at, market_type) "
        "VALUES ('nba', 'A', 'B', 'A', 58, 50, 8, 0, '', ?, 'h2h')",
        ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),),
    )
    conn.commit()
    conn.close()
    r = sd._simulate_alert_rule(_rule(), days=30, stake=100)
    assert r["n_bets"] == 0


def test_days_window_excludes_old_signals(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_signal(divergence=8.0, resolution="correct", detected_offset_days=200)
    _add_signal(divergence=8.0, resolution="correct", detected_offset_days=1)
    r = sd._simulate_alert_rule(_rule(), days=30, stake=100)
    assert r["n_bets"] == 1


def test_equity_curve_is_running_pnl(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_signal(divergence=8.0, poly_prob=50.0, resolution="correct",
                detected_offset_days=3)
    _add_signal(divergence=8.0, poly_prob=50.0, resolution="incorrect",
                detected_offset_days=2)
    _add_signal(divergence=8.0, poly_prob=50.0, resolution="correct",
                detected_offset_days=1)
    r = sd._simulate_alert_rule(_rule(), days=30, stake=100)
    # +100 -> 0 -> +100. Order is by detected_at ASC.
    assert r["equity_curve"] == [100.0, 0.0, 100.0]


def test_max_drawdown(tmp_path, monkeypatch):
    """Peak $200, drops to $0 -> max DD $200."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_signal(divergence=8.0, poly_prob=50.0, resolution="correct",
                detected_offset_days=4)
    _add_signal(divergence=8.0, poly_prob=50.0, resolution="correct",
                detected_offset_days=3)
    _add_signal(divergence=8.0, poly_prob=50.0, resolution="incorrect",
                detected_offset_days=2)
    _add_signal(divergence=8.0, poly_prob=50.0, resolution="incorrect",
                detected_offset_days=1)
    r = sd._simulate_alert_rule(_rule(), days=30, stake=100)
    assert r["max_drawdown"] == 200.0


def test_matches_capped_at_200(tmp_path, monkeypatch):
    _setup_isolated_db(tmp_path, monkeypatch)
    for i in range(250):
        _add_signal(divergence=8.0, resolution="correct",
                    detected_offset_days=1, commence_offset_hours=2)
    r = sd._simulate_alert_rule(_rule(), days=30, stake=100)
    assert r["n_bets"] == 250        # all counted in aggregates
    assert len(r["matches"]) == 200  # but only first 200 returned


def test_skips_rows_with_unusable_poly_prob(tmp_path, monkeypatch):
    """Rows with poly_prob 0 or 100 can't have a PnL computed (divide by
    zero or trivial)."""
    _setup_isolated_db(tmp_path, monkeypatch)
    _add_signal(divergence=8.0, poly_prob=0.0, resolution="correct")
    _add_signal(divergence=8.0, poly_prob=100.0, resolution="correct")
    _add_signal(divergence=8.0, poly_prob=50.0, resolution="correct")
    r = sd._simulate_alert_rule(_rule(), days=30, stake=100)
    assert r["n_bets"] == 1
