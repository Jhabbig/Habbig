"""Tests for the insight ledger — persistence, resolution, calibration,
and auto-mode candidate selection.

End-to-end on an in-memory SQLite; no LLM calls anywhere. The streaming
endpoint's logging integration is exercised by mocking the
`stream_insight` generator in the trade-engine fixture style.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading

import pytest

import insight_storage as ist


INSIGHT_SCHEMA = """
CREATE TABLE IF NOT EXISTS insight_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id             TEXT NOT NULL,
    generated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    model                 TEXT NOT NULL,
    mode                  TEXT NOT NULL,
    yes_price             REAL,
    model_prob            REAL,
    edge                  REAL,
    recommendation        TEXT,
    confidence            TEXT,
    suggested_limit_cents INTEGER,
    tail_warning          INTEGER DEFAULT 0,
    headline              TEXT,
    context_json          TEXT NOT NULL,
    insight_json          TEXT NOT NULL,
    usage_input_tokens    INTEGER DEFAULT 0,
    usage_output_tokens   INTEGER DEFAULT 0,
    usage_cache_creation  INTEGER DEFAULT 0,
    usage_cache_read      INTEGER DEFAULT 0,
    stop_reason           TEXT,
    latency_ms            INTEGER,
    triggered_by          TEXT NOT NULL DEFAULT 'user'
);
CREATE TABLE IF NOT EXISTS insight_resolutions (
    insight_id      INTEGER PRIMARY KEY,
    market_id       TEXT NOT NULL,
    actual_outcome  TEXT NOT NULL,
    was_correct     INTEGER,
    pnl_per_dollar  REAL,
    resolved_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weather_resolutions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT UNIQUE,
    actual_outcome  TEXT,
    payout          REAL,
    resolved_at     TEXT
);
"""


def _make_factory():
    """In-memory factory matching the production `_get_conn` shape."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(INSIGHT_SCHEMA)
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


def _make_complete(recommendation="BUY_YES", confidence="high",
                    suggested=66, tail_warning=False, headline="x"):
    """Helper to produce the `complete` chunk shape that
    `stream_insight()` yields."""
    return {
        "insight": {
            "recommendation": recommendation,
            "confidence": confidence,
            "headline": headline,
            "key_facts": ["fact"],
            "key_risks": ["risk"],
            "suggested_limit_cents": suggested,
            "tail_warning": tail_warning,
            "disclaimer": "Not investment advice.",
        },
        "usage": {
            "input_tokens": 5000, "output_tokens": 300,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 4500,
        },
        "model": "claude-haiku-4-5",
        "stop_reason": "end_turn",
    }


def _ctx(market_id="m1", yes_price=0.6, model_prob=0.78, edge=0.18):
    return {"market_id": market_id, "yes_price": yes_price,
            "model_prob": model_prob, "edge": edge,
            "question": "Will NYC be above 75°F?", "city": "nyc",
            "target_date": "2026-05-08"}


# ─── log_insight write path ───────────────────────────────────────────────────

def test_log_insight_persists_denormalized_columns():
    factory, conn = _make_factory()
    rid = ist.log_insight(
        factory, market_id="m1", context=_ctx(),
        complete_data=_make_complete(suggested=66, tail_warning=True),
        model="claude-haiku-4-5", mode="fast", latency_ms=1200,
        triggered_by="user",
    )
    assert rid is not None
    row = conn.execute(
        "SELECT * FROM insight_log WHERE id = ?", (rid,)
    ).fetchone()
    assert row["market_id"] == "m1"
    assert row["recommendation"] == "BUY_YES"
    assert row["confidence"] == "high"
    assert row["suggested_limit_cents"] == 66
    assert row["tail_warning"] == 1
    assert row["yes_price"] == 0.6
    assert row["edge"] == 0.18
    assert row["usage_cache_read"] == 4500
    assert row["triggered_by"] == "user"
    assert row["latency_ms"] == 1200


def test_log_insight_returns_none_on_failure():
    """A bad conn_factory raises inside log_insight; the function must
    swallow it (the streaming endpoint depends on this) and return None."""
    def broken_factory(readonly=False):
        raise sqlite3.OperationalError("disk full")
    rid = ist.log_insight(
        broken_factory, market_id="m1", context={},
        complete_data=_make_complete(), model="x", mode="fast",
    )
    assert rid is None


def test_log_insight_handles_missing_fields_in_complete():
    """The model could theoretically return malformed JSON that
    structured-outputs lets through during a beta; the persistence layer
    must not crash."""
    factory, _ = _make_factory()
    rid = ist.log_insight(
        factory, market_id="m1", context={},
        complete_data={"insight": None, "usage": {}},
        model="claude-haiku-4-5", mode="fast",
    )
    assert rid is not None


# ─── recent_insights / feed ───────────────────────────────────────────────────

def test_recent_insights_returns_newest_first():
    factory, _ = _make_factory()
    for i in range(3):
        ist.log_insight(factory, market_id=f"m{i}", context=_ctx(market_id=f"m{i}"),
                         complete_data=_make_complete(), model="x", mode="fast")
    feed = ist.recent_insights(factory, limit=10)
    assert len(feed) == 3
    assert feed[0]["id"] > feed[-1]["id"]


def test_recent_insights_filters_by_edge():
    factory, _ = _make_factory()
    ist.log_insight(factory, market_id="m_small",
                     context=_ctx(market_id="m_small", edge=0.02),
                     complete_data=_make_complete(), model="x", mode="fast")
    ist.log_insight(factory, market_id="m_big",
                     context=_ctx(market_id="m_big", edge=0.20),
                     complete_data=_make_complete(), model="x", mode="fast")
    big_only = ist.recent_insights(factory, min_abs_edge=0.10)
    assert {r["market_id"] for r in big_only} == {"m_big"}


def test_recent_insights_filters_by_recommendation():
    factory, _ = _make_factory()
    ist.log_insight(factory, market_id="m1", context=_ctx(),
                     complete_data=_make_complete(recommendation="BUY_YES"),
                     model="x", mode="fast")
    ist.log_insight(factory, market_id="m2", context=_ctx(market_id="m2"),
                     complete_data=_make_complete(recommendation="PASS"),
                     model="x", mode="fast")
    buys = ist.recent_insights(factory, recommendation="BUY_YES")
    assert [r["market_id"] for r in buys] == ["m1"]


# ─── insights_for_market / replay ─────────────────────────────────────────────

def test_insights_for_market_returns_full_insight_json():
    factory, _ = _make_factory()
    ist.log_insight(factory, market_id="m1", context=_ctx(),
                     complete_data=_make_complete(headline="first call"),
                     model="x", mode="fast")
    ist.log_insight(factory, market_id="m1", context=_ctx(yes_price=0.55),
                     complete_data=_make_complete(headline="second call"),
                     model="x", mode="fast")
    history = ist.insights_for_market(factory, "m1")
    assert len(history) == 2
    headlines = [h["insight"]["headline"] for h in history]
    assert "first call" in headlines and "second call" in headlines


# ─── has_recent_insight / de-dup ──────────────────────────────────────────────

def test_has_recent_insight_true_after_log():
    factory, _ = _make_factory()
    assert ist.has_recent_insight(factory, "m1", hours=6.0) is False
    ist.log_insight(factory, market_id="m1", context=_ctx(),
                     complete_data=_make_complete(), model="x", mode="fast")
    assert ist.has_recent_insight(factory, "m1", hours=6.0) is True


def test_has_recent_insight_false_for_old_row():
    """Manually backdate a row past the dedup window and confirm the
    helper returns False."""
    factory, conn = _make_factory()
    rid = ist.log_insight(factory, market_id="m1", context=_ctx(),
                           complete_data=_make_complete(), model="x", mode="fast")
    conn.execute(
        "UPDATE insight_log SET generated_at = datetime('now', '-12 hours') WHERE id = ?",
        (rid,),
    )
    conn.commit()
    assert ist.has_recent_insight(factory, "m1", hours=6.0) is False


# ─── PnL math ─────────────────────────────────────────────────────────────────

def test_pnl_buy_yes_wins():
    pnl = ist._bet_pnl_per_dollar("BUY_YES", 60, 0.5, "YES")
    assert pnl == pytest.approx(0.40)


def test_pnl_buy_yes_loses():
    pnl = ist._bet_pnl_per_dollar("BUY_YES", 60, 0.5, "NO")
    assert pnl == pytest.approx(-0.60)


def test_pnl_buy_no_wins():
    pnl = ist._bet_pnl_per_dollar("BUY_NO", 30, 0.7, "NO")
    assert pnl == pytest.approx(0.70)


def test_pnl_buy_no_loses():
    pnl = ist._bet_pnl_per_dollar("BUY_NO", 30, 0.7, "YES")
    assert pnl == pytest.approx(-0.30)


def test_pnl_pass_returns_zero():
    assert ist._bet_pnl_per_dollar("PASS", None, 0.5, "YES") == 0.0
    assert ist._bet_pnl_per_dollar("WAIT_AND_SEE", None, 0.5, "YES") == 0.0


def test_pnl_falls_back_to_market_price_when_suggested_missing():
    """An older row might lack suggested_limit_cents; fall back to the
    market price at gen time so PnL is still computable."""
    pnl = ist._bet_pnl_per_dollar("BUY_YES", None, 0.55, "YES")
    assert pnl == pytest.approx(0.45)


def test_was_correct():
    assert ist._was_correct("BUY_YES", "YES") == 1
    assert ist._was_correct("BUY_YES", "NO") == 0
    assert ist._was_correct("BUY_NO", "NO") == 1
    assert ist._was_correct("BUY_NO", "YES") == 0
    assert ist._was_correct("PASS", "YES") is None
    assert ist._was_correct("WAIT_AND_SEE", "NO") is None


# ─── resolve_insights ─────────────────────────────────────────────────────────

def _seed_resolved(conn, market_id, outcome):
    conn.execute(
        "INSERT OR REPLACE INTO weather_resolutions (market_id, actual_outcome, payout, resolved_at)"
        " VALUES (?, ?, ?, '2026-05-09T03:00:00Z')",
        (market_id, outcome, 1.0 if outcome == "YES" else 0.0),
    )
    conn.commit()


def test_resolve_insights_pairs_buy_yes_winner():
    factory, conn = _make_factory()
    rid = ist.log_insight(factory, market_id="m1", context=_ctx(),
                           complete_data=_make_complete(recommendation="BUY_YES",
                                                         suggested=66),
                           model="x", mode="fast")
    _seed_resolved(conn, "m1", "YES")
    stats = ist.resolve_insights(factory)
    assert stats["resolved"] == 1
    row = conn.execute("SELECT * FROM insight_resolutions WHERE insight_id = ?",
                        (rid,)).fetchone()
    assert row["actual_outcome"] == "YES"
    assert row["was_correct"] == 1
    assert row["pnl_per_dollar"] == pytest.approx(0.34)


def test_resolve_insights_idempotent():
    factory, conn = _make_factory()
    ist.log_insight(factory, market_id="m1", context=_ctx(),
                     complete_data=_make_complete(), model="x", mode="fast")
    _seed_resolved(conn, "m1", "YES")
    s1 = ist.resolve_insights(factory)
    s2 = ist.resolve_insights(factory)
    assert s1["resolved"] == 1
    assert s2["resolved"] == 0  # already paired


def test_resolve_insights_skips_unresolved_markets():
    factory, _ = _make_factory()
    ist.log_insight(factory, market_id="m1", context=_ctx(),
                     complete_data=_make_complete(), model="x", mode="fast")
    stats = ist.resolve_insights(factory)
    assert stats["resolved"] == 0


def test_resolve_insights_pass_recommendation_has_null_was_correct():
    factory, conn = _make_factory()
    ist.log_insight(factory, market_id="m1", context=_ctx(),
                     complete_data=_make_complete(recommendation="PASS",
                                                   suggested=None),
                     model="x", mode="fast")
    _seed_resolved(conn, "m1", "YES")
    ist.resolve_insights(factory)
    row = conn.execute("SELECT was_correct, pnl_per_dollar FROM insight_resolutions").fetchone()
    assert row["was_correct"] is None
    assert row["pnl_per_dollar"] == 0.0


# ─── calibration_stats ────────────────────────────────────────────────────────

def test_calibration_stats_brier_perfect_predictions():
    factory, conn = _make_factory()
    # 5 BUY_YES that all hit YES + 5 BUY_NO that all hit NO → Brier 0
    for i in range(5):
        ist.log_insight(factory, market_id=f"y{i}",
                         context=_ctx(market_id=f"y{i}"),
                         complete_data=_make_complete(recommendation="BUY_YES",
                                                       suggested=50),
                         model="x", mode="fast")
        _seed_resolved(conn, f"y{i}", "YES")
        ist.log_insight(factory, market_id=f"n{i}",
                         context=_ctx(market_id=f"n{i}"),
                         complete_data=_make_complete(recommendation="BUY_NO",
                                                       suggested=50),
                         model="x", mode="fast")
        _seed_resolved(conn, f"n{i}", "NO")
    ist.resolve_insights(factory)
    cal = ist.calibration_stats(factory)
    assert cal["brier_score"] == 0.0
    assert cal["win_rate"] == 1.0
    assert cal["n_brier_samples"] == 10
    assert cal["by_recommendation"]["BUY_YES"]["wins"] == 5
    assert cal["by_recommendation"]["BUY_NO"]["wins"] == 5


def test_calibration_stats_handles_mixed_outcomes():
    factory, conn = _make_factory()
    # 1 BUY_YES on YES (win, +50¢), 1 BUY_YES on NO (loss, -50¢)
    ist.log_insight(factory, market_id="a", context=_ctx(market_id="a"),
                     complete_data=_make_complete(recommendation="BUY_YES",
                                                   suggested=50),
                     model="x", mode="fast")
    _seed_resolved(conn, "a", "YES")
    ist.log_insight(factory, market_id="b", context=_ctx(market_id="b"),
                     complete_data=_make_complete(recommendation="BUY_YES",
                                                   suggested=50),
                     model="x", mode="fast")
    _seed_resolved(conn, "b", "NO")
    ist.resolve_insights(factory)
    cal = ist.calibration_stats(factory)
    assert cal["win_rate"] == 0.5
    assert cal["brier_score"] == 0.5  # (1-1)^2 + (1-0)^2 averaged = 0.5
    assert cal["total_pnl_per_dollar"] == 0.0


def test_calibration_stats_confidence_breakdown():
    factory, conn = _make_factory()
    # 2 high-confidence calls, both correct
    for i in range(2):
        ist.log_insight(factory, market_id=f"h{i}",
                         context=_ctx(market_id=f"h{i}"),
                         complete_data=_make_complete(confidence="high"),
                         model="x", mode="fast")
        _seed_resolved(conn, f"h{i}", "YES")
    # 1 low-confidence call, wrong
    ist.log_insight(factory, market_id="L",
                     context=_ctx(market_id="L"),
                     complete_data=_make_complete(confidence="low"),
                     model="x", mode="fast")
    _seed_resolved(conn, "L", "NO")
    ist.resolve_insights(factory)
    cal = ist.calibration_stats(factory)
    assert cal["by_confidence"]["high"]["win_rate"] == 1.0
    assert cal["by_confidence"]["low"]["win_rate"] == 0.0


def test_calibration_stats_tail_warning_tracking():
    factory, conn = _make_factory()
    ist.log_insight(factory, market_id="t", context=_ctx(market_id="t"),
                     complete_data=_make_complete(tail_warning=True),
                     model="x", mode="fast")
    _seed_resolved(conn, "t", "YES")
    ist.resolve_insights(factory)
    cal = ist.calibration_stats(factory)
    assert cal["tail_warning_calls"] == 1
    assert cal["tail_warning_win_rate"] == 1.0


def test_calibration_stats_empty():
    factory, _ = _make_factory()
    cal = ist.calibration_stats(factory)
    assert cal["n_total"] == 0
    assert cal["brier_score"] is None
    assert cal["win_rate"] is None


# ─── auto_candidates ──────────────────────────────────────────────────────────

def _market(market_id, edge, target_date="2026-12-31"):
    return {"market_id": market_id, "edge": edge,
            "target_date": target_date, "yes_price": 0.5}


def test_auto_candidates_filters_below_threshold():
    factory, _ = _make_factory()
    markets = [_market("m1", 0.02), _market("m2", 0.06), _market("m3", 0.15)]
    out = ist.auto_candidates(factory, markets, min_abs_edge=0.05)
    ids = {m["market_id"] for m in out}
    assert ids == {"m2", "m3"}


def test_auto_candidates_sorts_by_absolute_edge_descending():
    factory, _ = _make_factory()
    markets = [_market("small", 0.06), _market("big", 0.20),
               _market("mid", 0.10), _market("neg_big", -0.18)]
    out = ist.auto_candidates(factory, markets, min_abs_edge=0.05, limit=10)
    # |edge| order: 0.20, 0.18, 0.10, 0.06
    assert [m["market_id"] for m in out] == ["big", "neg_big", "mid", "small"]


def test_auto_candidates_respects_limit():
    factory, _ = _make_factory()
    markets = [_market(f"m{i}", 0.10 + i * 0.01) for i in range(20)]
    out = ist.auto_candidates(factory, markets, min_abs_edge=0.05, limit=3)
    assert len(out) == 3


def test_auto_candidates_excludes_recent():
    """Markets that already got an insight in the dedup window are
    skipped — auto-mode shouldn't generate duplicates."""
    factory, _ = _make_factory()
    ist.log_insight(factory, market_id="recently",
                     context=_ctx(market_id="recently"),
                     complete_data=_make_complete(), model="x", mode="fast")
    markets = [_market("recently", 0.20), _market("fresh", 0.10)]
    out = ist.auto_candidates(factory, markets, min_abs_edge=0.05,
                               dedup_hours=6.0)
    assert {m["market_id"] for m in out} == {"fresh"}


def test_auto_candidates_skips_past_dates():
    """Markets that already resolved (target_date in past) shouldn't
    burn budget on new insights."""
    factory, _ = _make_factory()
    markets = [_market("yesterday", 0.20, target_date="2020-01-01"),
               _market("future", 0.10, target_date="2099-12-31")]
    out = ist.auto_candidates(factory, markets)
    assert [m["market_id"] for m in out] == ["future"]


def test_auto_candidates_handles_empty_input():
    factory, _ = _make_factory()
    assert ist.auto_candidates(factory, []) == []
    assert ist.auto_candidates(factory, None) == []
