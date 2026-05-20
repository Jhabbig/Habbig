"""Tests for the public-track-record module.

Covers the three load-bearing properties:
  * Resolver fills `weather_resolutions` correctly and is idempotent.
  * Rollup builder produces deterministic content hashes for the
    same input, so external auditors can recompute them.
  * The chain breaks loudly when any link (content, prev_hash, HMAC)
    is tampered with.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from unittest.mock import patch

import pytest

import track_record as track


PHASE3_SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_signals_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT,
    question    TEXT,
    category    TEXT,
    yes_price   REAL,
    model_prob  REAL,
    edge        REAL,
    action      TEXT,
    timestamp   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS weather_resolutions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT UNIQUE,
    actual_outcome  TEXT,
    payout          REAL,
    resolved_at     TEXT
);
CREATE TABLE IF NOT EXISTS weather_price_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    source      TEXT,
    question    TEXT,
    city        TEXT,
    target_date TEXT,
    yes_price   REAL,
    model_prob  REAL,
    edge        REAL,
    volume      REAL DEFAULT 0,
    timestamp   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS track_record_rollups (
    date          TEXT PRIMARY KEY,
    payload       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    prev_hash     TEXT NOT NULL,
    hmac_sig      TEXT NOT NULL,
    committed_at  TEXT NOT NULL
);
"""


def _make_factory():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(PHASE3_SCHEMA)
    lock = threading.Lock()

    @contextlib.contextmanager
    def factory(readonly=False):
        with lock:
            try:
                yield conn
                if not readonly:
                    conn.commit()
            except Exception:
                if not readonly:
                    conn.rollback()
                raise

    return factory, conn


def _seed_signal(conn, market_id, question, model_prob, edge,
                 city, target_date, outcome=None, ts="2026-05-01T12:00:00.000Z",
                 category="temperature"):
    conn.execute(
        "INSERT INTO weather_signals_log (market_id, question, category, model_prob, edge, yes_price, timestamp)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (market_id, question, category, model_prob, edge, 0.5, ts),
    )
    conn.execute(
        "INSERT INTO weather_price_snapshots (market_id, question, city, target_date, yes_price, model_prob, edge)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (market_id, question, city, target_date, 0.5, model_prob, edge),
    )
    if outcome:
        conn.execute(
            "INSERT INTO weather_resolutions (market_id, actual_outcome, payout, resolved_at)"
            " VALUES (?, ?, ?, ?)",
            (market_id, outcome, 1.0 if outcome == "YES" else 0.0, "2026-05-02T03:00:00Z"),
        )
    conn.commit()


# ─── Resolver ─────────────────────────────────────────────────────────────────

def test_resolver_writes_yes_when_observed_above_threshold():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "Will the high be above 70?", 0.6, 0.1,
                 "nyc", "2025-01-01")
    with patch("track_record._fetch_observed_high", return_value=75.0):
        stats = track.resolve_signals(factory, {"nyc": (40.77, -73.87)})
    assert stats["resolved"] == 1
    row = conn.execute("SELECT * FROM weather_resolutions WHERE market_id='m1'").fetchone()
    assert row["actual_outcome"] == "YES"


def test_resolver_writes_no_when_below():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "Will the high be above 70?", 0.6, 0.1,
                 "nyc", "2025-01-01")
    with patch("track_record._fetch_observed_high", return_value=65.0):
        track.resolve_signals(factory, {"nyc": (40.77, -73.87)})
    row = conn.execute("SELECT actual_outcome FROM weather_resolutions WHERE market_id='m1'").fetchone()
    assert row["actual_outcome"] == "NO"


def test_resolver_idempotent():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "Will the high be above 70?", 0.6, 0.1,
                 "nyc", "2025-01-01")
    with patch("track_record._fetch_observed_high", return_value=75.0):
        track.resolve_signals(factory, {"nyc": (40.77, -73.87)})
        # Second pass should not find any unresolved rows
        stats2 = track.resolve_signals(factory, {"nyc": (40.77, -73.87)})
    assert stats2["resolved"] == 0


def test_resolver_skips_signals_without_threshold():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "Will it rain tomorrow?", 0.5, 0.0,
                 "nyc", "2025-01-01")
    with patch("track_record._fetch_observed_high", return_value=75.0):
        stats = track.resolve_signals(factory, {"nyc": (40.77, -73.87)})
    assert stats["resolved"] == 0
    assert stats["skipped_no_threshold"] == 1


def test_resolver_skips_future_dates():
    """A signal for a market resolving tomorrow should not be touched."""
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "Will the high be above 70?", 0.6, 0.1,
                 "nyc", future)
    with patch("track_record._fetch_observed_high", return_value=75.0):
        stats = track.resolve_signals(factory, {"nyc": (40.77, -73.87)})
    assert stats["resolved"] == 0


def test_resolver_skips_unknown_city():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "Will the high be above 70?", 0.6, 0.1,
                 "atlantis", "2025-01-01")
    stats = track.resolve_signals(factory, {"nyc": (40.77, -73.87)})
    assert stats["skipped_no_city"] == 1


# ─── Rollup builder ───────────────────────────────────────────────────────────

def test_rollup_aggregates_signals_on_date():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "above 70", 0.6, 0.1, "nyc", "2026-05-01",
                 outcome="YES", ts="2026-05-01T10:00:00.000Z")
    _seed_signal(conn, "m2", "above 80", 0.3, -0.1, "nyc", "2026-05-01",
                 outcome="NO", ts="2026-05-01T11:00:00.000Z")
    rollup = track.build_daily_rollup(factory, "2026-05-01")
    assert rollup.payload["n_signals"] == 2
    assert rollup.payload["n_resolved"] == 2
    # m1: edge=+0.1 (YES bet) won; m2: edge=-0.1 (NO bet) won
    assert rollup.payload["win_rate"] == 1.0


def test_rollup_content_hash_is_deterministic():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "above 70", 0.6, 0.1, "nyc", "2026-05-01",
                 outcome="YES", ts="2026-05-01T10:00:00.000Z")
    rollup_a = track.build_daily_rollup(factory, "2026-05-01")
    rollup_b = track.build_daily_rollup(factory, "2026-05-01")
    # generated_at differs between calls — exclude it from the hash check
    a = {k: v for k, v in rollup_a.payload.items() if k != "generated_at"}
    b = {k: v for k, v in rollup_b.payload.items() if k != "generated_at"}
    assert a == b


def test_rollup_excludes_signals_without_model_prob():
    factory, conn = _make_factory()
    conn.execute(
        "INSERT INTO weather_signals_log (market_id, question, model_prob, timestamp)"
        " VALUES ('m1', 'q', NULL, '2026-05-01T10:00:00Z')")
    conn.commit()
    rollup = track.build_daily_rollup(factory, "2026-05-01")
    assert rollup.payload["n_signals"] == 0


def test_rollup_handles_empty_day():
    factory, _ = _make_factory()
    rollup = track.build_daily_rollup(factory, "2026-05-01")
    assert rollup.payload["n_signals"] == 0
    assert rollup.payload["brier_score"] is None
    assert rollup.payload["reliability"] == []


def test_rollup_category_breakdown():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "above 70", 0.6, 0.1, "nyc", "2026-05-01",
                 outcome="YES", ts="2026-05-01T10:00:00.000Z", category="temperature")
    _seed_signal(conn, "m2", "above 80", 0.4, -0.1, "nyc", "2026-05-01",
                 outcome="YES", ts="2026-05-01T11:00:00.000Z", category="temperature")
    rollup = track.build_daily_rollup(factory, "2026-05-01")
    cats = rollup.payload["categories"]
    assert "temperature" in cats
    assert cats["temperature"]["n_total"] == 2
    assert cats["temperature"]["n_resolved"] == 2


# ─── Chain commitment + verification ──────────────────────────────────────────

SECRET = b"test-secret-key-for-tests-only"


def test_commit_first_rollup_uses_genesis_prev_hash():
    factory, _ = _make_factory()
    rollup = track.build_daily_rollup(factory, "2026-05-01")
    committed = track.commit_rollup(factory, rollup, SECRET)
    assert committed["prev_hash"] == track.GENESIS_HASH


def test_commit_second_rollup_chains_to_first():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "above 70", 0.6, 0.1, "nyc", "2026-05-01",
                 outcome="YES", ts="2026-05-01T10:00:00.000Z")
    _seed_signal(conn, "m2", "above 75", 0.55, 0.05, "nyc", "2026-05-02",
                 outcome="YES", ts="2026-05-02T10:00:00.000Z")
    a = track.commit_rollup(factory, track.build_daily_rollup(factory, "2026-05-01"), SECRET)
    b = track.commit_rollup(factory, track.build_daily_rollup(factory, "2026-05-02"), SECRET)
    assert b["prev_hash"] == a["content_hash"]


def test_commit_refuses_duplicate_date():
    factory, _ = _make_factory()
    rollup = track.build_daily_rollup(factory, "2026-05-01")
    track.commit_rollup(factory, rollup, SECRET)
    with pytest.raises(ValueError, match="already committed"):
        track.commit_rollup(factory, track.build_daily_rollup(factory, "2026-05-01"), SECRET)


def test_verify_clean_chain():
    factory, conn = _make_factory()
    for i, d in enumerate(["2026-05-01", "2026-05-02", "2026-05-03"]):
        _seed_signal(conn, f"m{i}", "above 70", 0.6, 0.1, "nyc", d,
                     outcome="YES", ts=f"{d}T10:00:00.000Z")
        track.commit_rollup(factory, track.build_daily_rollup(factory, d), SECRET)
    result = track.verify_chain(factory, SECRET)
    assert result["ok"] is True
    assert result["n_rows"] == 3
    assert result["errors"] == []


def test_verify_detects_tampered_payload():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "above 70", 0.6, 0.1, "nyc", "2026-05-01",
                 outcome="YES", ts="2026-05-01T10:00:00.000Z")
    track.commit_rollup(factory, track.build_daily_rollup(factory, "2026-05-01"), SECRET)
    # Manually edit the stored payload — content_hash no longer matches
    conn.execute(
        "UPDATE track_record_rollups SET payload = ? WHERE date = ?",
        ('{"hacked": true}', "2026-05-01"),
    )
    conn.commit()
    result = track.verify_chain(factory, SECRET)
    assert result["ok"] is False
    assert result["first_bad_date"] == "2026-05-01"
    assert result["errors"][0]["error"] == "content_hash_mismatch"


def test_verify_detects_tampered_prev_hash():
    factory, conn = _make_factory()
    for d in ["2026-05-01", "2026-05-02"]:
        _seed_signal(conn, f"m_{d}", "above 70", 0.6, 0.1, "nyc", d,
                     outcome="YES", ts=f"{d}T10:00:00.000Z")
        track.commit_rollup(factory, track.build_daily_rollup(factory, d), SECRET)
    # Break the link between day 1 and day 2
    conn.execute(
        "UPDATE track_record_rollups SET prev_hash = 'forged' WHERE date = '2026-05-02'"
    )
    conn.commit()
    result = track.verify_chain(factory, SECRET)
    assert result["ok"] is False
    assert result["first_bad_date"] == "2026-05-02"


def test_verify_detects_wrong_hmac_key():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "above 70", 0.6, 0.1, "nyc", "2026-05-01",
                 outcome="YES", ts="2026-05-01T10:00:00.000Z")
    track.commit_rollup(factory, track.build_daily_rollup(factory, "2026-05-01"), SECRET)
    result = track.verify_chain(factory, b"different-secret")
    assert result["ok"] is False
    assert result["errors"][0]["error"] == "hmac_mismatch"


def test_manifest_lists_rollups_newest_first():
    factory, conn = _make_factory()
    for d in ["2026-05-01", "2026-05-02", "2026-05-03"]:
        _seed_signal(conn, f"m_{d}", "above 70", 0.6, 0.1, "nyc", d,
                     outcome="YES", ts=f"{d}T10:00:00.000Z")
        track.commit_rollup(factory, track.build_daily_rollup(factory, d), SECRET)
    manifest = track.list_rollups(factory)
    assert [r["date"] for r in manifest] == ["2026-05-03", "2026-05-02", "2026-05-01"]


def test_get_rollup_returns_parsed_payload():
    factory, conn = _make_factory()
    _seed_signal(conn, "m1", "above 70", 0.6, 0.1, "nyc", "2026-05-01",
                 outcome="YES", ts="2026-05-01T10:00:00.000Z")
    track.commit_rollup(factory, track.build_daily_rollup(factory, "2026-05-01"), SECRET)
    rollup = track.get_rollup(factory, "2026-05-01")
    assert rollup is not None
    assert isinstance(rollup["payload"], dict)
    assert rollup["payload"]["date"] == "2026-05-01"


def test_get_rollup_unknown_date_returns_none():
    factory, _ = _make_factory()
    assert track.get_rollup(factory, "2099-12-31") is None


# ─── Lifetime summary ─────────────────────────────────────────────────────────

def test_lifetime_summary_aggregates_across_all_dates():
    factory, conn = _make_factory()
    for i, d in enumerate(["2026-05-01", "2026-05-02"]):
        _seed_signal(conn, f"m{i}_a", "above 70", 0.6, 0.1, "nyc", d,
                     outcome="YES", ts=f"{d}T10:00:00.000Z")
        _seed_signal(conn, f"m{i}_b", "above 80", 0.3, -0.1, "nyc", d,
                     outcome="YES", ts=f"{d}T11:00:00.000Z")
    summary = track.lifetime_summary(factory)
    assert summary["n_total"] == 4
    assert summary["n_resolved"] == 4
    # m_a (edge +0.1, YES): win. m_b (edge -0.1, NO bet, YES wins): lose. So 50%.
    assert summary["win_rate"] == 0.5


# ─── Daily series / per-station / edge buckets ───────────────────────────────

def _seed_signal_with_price(conn, market_id, question, model_prob, edge,
                            yes_price, city, target_date, outcome,
                            ts="2026-05-01T12:00:00.000Z"):
    conn.execute(
        "INSERT INTO weather_signals_log (market_id, question, category, model_prob, edge, yes_price, timestamp)"
        " VALUES (?, ?, 'temperature', ?, ?, ?, ?)",
        (market_id, question, model_prob, edge, yes_price, ts),
    )
    conn.execute(
        "INSERT INTO weather_price_snapshots (market_id, question, city, target_date, yes_price, model_prob, edge)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (market_id, question, city, target_date, yes_price, model_prob, edge),
    )
    if outcome:
        conn.execute(
            "INSERT INTO weather_resolutions (market_id, actual_outcome, payout, resolved_at)"
            " VALUES (?, ?, ?, '2026-05-02T03:00:00Z')",
            (market_id, outcome, 1.0 if outcome == "YES" else 0.0),
        )
    conn.commit()


def test_daily_series_computes_cumulative_pnl_in_signal_direction():
    from datetime import datetime, timezone, timedelta
    factory, conn = _make_factory()
    # Recent timestamps so the "last 90 days" filter picks them up.
    today = datetime.now(timezone.utc).date()
    d1 = (today - timedelta(days=2)).isoformat()
    d2 = (today - timedelta(days=1)).isoformat()
    # d1: bet YES at 0.4, YES wins → +0.6
    _seed_signal_with_price(conn, "m1", "above 70", 0.6, 0.2, 0.4,
                            "nyc", d1, "YES", ts=f"{d1}T10:00:00.000Z")
    # d2: bet NO at 0.7 (edge negative), NO wins → +0.7
    _seed_signal_with_price(conn, "m2", "above 80", 0.2, -0.5, 0.7,
                            "nyc", d2, "NO", ts=f"{d2}T10:00:00.000Z")
    series = track.daily_series(factory, days=10)
    assert len(series) == 2
    # Both bets win — cumulative PnL should be positive and monotone
    assert series[1]["cumulative_pnl"] > series[0]["cumulative_pnl"]
    # The d1 PnL is exactly 0.6 (paid 0.4, won 1.0)
    assert series[0]["pnl"] == pytest.approx(0.6, abs=0.001)


def test_daily_series_filters_to_window():
    from datetime import datetime, timezone, timedelta
    factory, conn = _make_factory()
    today = datetime.now(timezone.utc).date()
    inside = (today - timedelta(days=3)).isoformat()
    outside = (today - timedelta(days=100)).isoformat()
    _seed_signal_with_price(conn, "m_in", "above 70", 0.6, 0.1, 0.5,
                            "nyc", inside, "YES", ts=f"{inside}T10:00:00.000Z")
    _seed_signal_with_price(conn, "m_out", "above 70", 0.6, 0.1, 0.5,
                            "nyc", outside, "YES", ts=f"{outside}T10:00:00.000Z")
    series = track.daily_series(factory, days=10)
    assert len(series) == 1
    assert series[0]["date"] == inside


def test_daily_series_includes_unresolved_signals_in_n_signals_only():
    """Unresolved signals count in n_signals but contribute zero PnL."""
    from datetime import datetime, timezone, timedelta
    factory, conn = _make_factory()
    today = datetime.now(timezone.utc).date()
    d = (today - timedelta(days=1)).isoformat()
    _seed_signal_with_price(conn, "m1", "above 70", 0.6, 0.1, 0.5,
                            "nyc", d, None, ts=f"{d}T10:00:00.000Z")
    series = track.daily_series(factory, days=10)
    assert len(series) == 1
    assert series[0]["n_signals"] == 1
    assert series[0]["n_resolved"] == 0
    assert series[0]["pnl"] == 0.0


def test_per_station_skill_groups_by_city():
    factory, conn = _make_factory()
    _seed_signal_with_price(conn, "m1", "above 70", 0.6, 0.1, 0.5,
                            "nyc", "2026-05-01", "YES")
    _seed_signal_with_price(conn, "m2", "above 80", 0.4, -0.1, 0.5,
                            "nyc", "2026-05-01", "NO")
    _seed_signal_with_price(conn, "m3", "above 65", 0.6, 0.1, 0.5,
                            "miami", "2026-05-01", "NO")  # bet YES, lost
    skill = track.per_station_skill(factory)
    by_city = {s["city"]: s for s in skill}
    assert by_city["nyc"]["n"] == 2
    assert by_city["nyc"]["win_rate"] == 1.0
    assert by_city["miami"]["n"] == 1
    assert by_city["miami"]["win_rate"] == 0.0


def test_per_station_skill_ignores_unresolved():
    factory, conn = _make_factory()
    _seed_signal_with_price(conn, "m1", "above 70", 0.6, 0.1, 0.5,
                            "nyc", "2026-05-01", None)
    skill = track.per_station_skill(factory)
    assert skill == []


def test_edge_buckets_count_and_win_rate():
    factory, conn = _make_factory()
    # 3 signals with edge=0.01 (in 0-2pp), all win
    for i in range(3):
        _seed_signal_with_price(conn, f"a{i}", "above 70", 0.51, 0.01, 0.5,
                                "nyc", "2026-05-01", "YES",
                                ts=f"2026-05-01T1{i}:00:00.000Z")
    # 2 signals with edge=0.10 (in 8-12pp), both win
    for i in range(2):
        _seed_signal_with_price(conn, f"b{i}", "above 70", 0.6, 0.1, 0.5,
                                "nyc", "2026-05-01", "YES",
                                ts=f"2026-05-01T2{i}:00:00.000Z")
    buckets = track.edge_bucket_win_rates(factory)
    by_label = {b["bucket"]: b for b in buckets}
    assert by_label["0–2pp"]["n"] == 3
    assert by_label["0–2pp"]["win_rate"] == 1.0
    assert by_label["8–12pp"]["n"] == 2
    assert by_label["8–12pp"]["win_rate"] == 1.0
    assert by_label["5–8pp"]["n"] == 0
    assert by_label["5–8pp"]["win_rate"] is None


def test_edge_buckets_returns_all_buckets_even_when_empty():
    factory, _ = _make_factory()
    buckets = track.edge_bucket_win_rates(factory)
    labels = [b["bucket"] for b in buckets]
    assert labels == ["0–2pp", "2–5pp", "5–8pp", "8–12pp", "12–20pp", "≥20pp"]
    assert all(b["n"] == 0 for b in buckets)
