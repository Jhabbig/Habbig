"""Tests for the head-to-head LLM vs raw-model comparison."""

from __future__ import annotations

import contextlib
import sqlite3
import threading

import pytest

import insight_compare as ic


SCHEMA = """
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
"""


def _make_factory():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
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


def _seed(conn, *, recommendation, edge, yes_price, outcome,
          suggested=None, market_id=None, was_correct=None,
          pnl_per_dollar=None):
    """Insert one paired (insight_log + insight_resolutions) row."""
    mid = market_id or f"m_{recommendation}_{edge}_{outcome}"
    cur = conn.execute(
        """INSERT INTO insight_log (market_id, model, mode, yes_price,
            edge, recommendation, confidence, suggested_limit_cents,
            context_json, insight_json)
           VALUES (?, 'haiku', 'fast', ?, ?, ?, 'high', ?, '{}', '{}')""",
        (mid, yes_price, edge, recommendation, suggested),
    )
    iid = cur.lastrowid
    conn.execute(
        """INSERT INTO insight_resolutions (insight_id, market_id, actual_outcome,
            was_correct, pnl_per_dollar, resolved_at)
           VALUES (?, ?, ?, ?, ?, '2026-05-08T03:00:00Z')""",
        (iid, mid, outcome, was_correct,
         pnl_per_dollar if pnl_per_dollar is not None else 0.0),
    )
    conn.commit()


# ─── raw_signal_call ──────────────────────────────────────────────────────────

def test_raw_signal_call_above_threshold():
    assert ic.raw_signal_call(0.10) == "BUY_YES"
    assert ic.raw_signal_call(-0.10) == "BUY_NO"
    assert ic.raw_signal_call(0.05) == "BUY_YES"  # boundary inclusive
    assert ic.raw_signal_call(-0.05) == "BUY_NO"


def test_raw_signal_call_below_threshold():
    assert ic.raw_signal_call(0.04) == "PASS"
    assert ic.raw_signal_call(-0.04) == "PASS"
    assert ic.raw_signal_call(0.0) == "PASS"


def test_raw_signal_call_custom_threshold():
    assert ic.raw_signal_call(0.06, threshold=0.10) == "PASS"
    assert ic.raw_signal_call(0.15, threshold=0.10) == "BUY_YES"


def test_raw_signal_call_handles_none_and_bad_values():
    assert ic.raw_signal_call(None) == "PASS"
    assert ic.raw_signal_call("not a number") == "PASS"


# ─── _bet_pnl ─────────────────────────────────────────────────────────────────

def test_bet_pnl_buy_yes_wins():
    assert ic._bet_pnl("BUY_YES", 0.40, "YES") == pytest.approx(0.60)


def test_bet_pnl_buy_no_wins():
    assert ic._bet_pnl("BUY_NO", 0.40, "NO") == pytest.approx(0.40)


def test_bet_pnl_pass_is_zero():
    assert ic._bet_pnl("PASS", 0.50, "YES") == 0.0
    assert ic._bet_pnl("WAIT_AND_SEE", 0.50, "YES") == 0.0


def test_bet_pnl_handles_missing_price():
    assert ic._bet_pnl("BUY_YES", None, "YES") is None


# ─── head_to_head_stats ───────────────────────────────────────────────────────

def test_head_to_head_returns_zero_n_on_empty():
    factory, _ = _make_factory()
    stats = ic.head_to_head_stats(factory)
    assert stats["n"] == 0
    assert stats["agreement_rate"] is None
    assert stats["raw"]["win_rate"] is None
    assert stats["llm"]["win_rate"] is None


def test_head_to_head_unanimous_buy_yes_wins():
    """Both raw signal and LLM say BUY_YES, market resolves YES — both win."""
    factory, conn = _make_factory()
    for i in range(3):
        _seed(conn, recommendation="BUY_YES", edge=0.10, yes_price=0.40,
              outcome="YES", suggested=45, market_id=f"m{i}",
              was_correct=1, pnl_per_dollar=0.55)
    stats = ic.head_to_head_stats(factory)
    assert stats["n"] == 3
    assert stats["agreement_rate"] == 1.0
    assert stats["raw"]["win_rate"] == 1.0
    assert stats["llm"]["win_rate"] == 1.0
    # Raw bets at 0.40, wins all three → 3 * 0.60 = 1.80
    assert stats["raw"]["total_pnl"] == pytest.approx(1.80)
    # LLM PnL is whatever we stored (pnl_per_dollar=0.55 × 3)
    assert stats["llm"]["total_pnl"] == pytest.approx(1.65)


def test_head_to_head_disagreement_breakdown():
    """3 markets where raw says BUY_YES (positive edge) but LLM says PASS.
    1 wins, 2 lose. Raw should be 33% on the bets; LLM 0 bets / 0 wins."""
    factory, conn = _make_factory()
    _seed(conn, recommendation="PASS", edge=0.10, yes_price=0.50,
          outcome="YES", market_id="d1", was_correct=None, pnl_per_dollar=0.0)
    _seed(conn, recommendation="PASS", edge=0.10, yes_price=0.50,
          outcome="NO", market_id="d2", was_correct=None, pnl_per_dollar=0.0)
    _seed(conn, recommendation="PASS", edge=0.10, yes_price=0.50,
          outcome="NO", market_id="d3", was_correct=None, pnl_per_dollar=0.0)
    stats = ic.head_to_head_stats(factory)
    assert stats["n"] == 3
    assert stats["agreement_rate"] == 0.0  # raw=BUY_YES, llm=PASS for all
    # Raw bet 3 times, won once
    assert stats["raw"]["n_betted"] == 3
    assert stats["raw"]["win_rate"] == pytest.approx(1 / 3, abs=0.01)
    # LLM never bet
    assert stats["llm"]["n_betted"] == 0
    assert stats["llm"]["win_rate"] is None
    # Disagreement: 3 disagreements, only raw bet
    assert stats["when_disagree"]["n"] == 3
    assert stats["when_disagree"]["n_betted"] == 3
    assert stats["when_disagree"]["raw_win_rate"] == pytest.approx(1 / 3, abs=0.01)
    assert stats["when_disagree"]["llm_win_rate"] == 0.0


def test_head_to_head_matrix_buckets_correctly():
    """Verify the (raw_call, llm_call) cells get filled and counts add up."""
    factory, conn = _make_factory()
    # Cell (BUY_YES, BUY_YES) — unanimous bull
    _seed(conn, recommendation="BUY_YES", edge=0.20, yes_price=0.40,
          outcome="YES", was_correct=1, pnl_per_dollar=0.55, market_id="a")
    # Cell (PASS, PASS) — unanimous skip on a thin edge
    _seed(conn, recommendation="PASS", edge=0.01, yes_price=0.50,
          outcome="YES", was_correct=None, pnl_per_dollar=0.0, market_id="b")
    # Cell (BUY_YES, BUY_NO) — wild disagreement
    _seed(conn, recommendation="BUY_NO", edge=0.10, yes_price=0.40,
          outcome="NO", was_correct=1, pnl_per_dollar=0.40, market_id="c")
    stats = ic.head_to_head_stats(factory)
    assert stats["matrix"]["BUY_YES"]["BUY_YES"]["n"] == 1
    assert stats["matrix"]["PASS"]["PASS"]["n"] == 1
    assert stats["matrix"]["BUY_YES"]["BUY_NO"]["n"] == 1
    # In the disagreement cell raw was wrong (BUY_YES vs NO outcome),
    # LLM was right (BUY_NO vs NO outcome).
    cell = stats["matrix"]["BUY_YES"]["BUY_NO"]
    assert cell["raw_wins"] == 0
    assert cell["llm_wins"] == 1


def test_head_to_head_filters_by_days_window():
    factory, conn = _make_factory()
    # An old row, backdated past the default window
    cur = conn.execute(
        """INSERT INTO insight_log (market_id, model, mode, yes_price, edge,
            recommendation, generated_at, context_json, insight_json)
           VALUES ('old', 'haiku', 'fast', 0.5, 0.10, 'BUY_YES',
                   datetime('now', '-300 days'), '{}', '{}')"""
    )
    iid = cur.lastrowid
    conn.execute(
        """INSERT INTO insight_resolutions (insight_id, market_id, actual_outcome,
            was_correct, pnl_per_dollar, resolved_at)
           VALUES (?, 'old', 'YES', 1, 0.5, '2024-01-01T00:00:00Z')""",
        (iid,),
    )
    conn.commit()
    _seed(conn, recommendation="BUY_YES", edge=0.10, yes_price=0.40,
          outcome="YES", was_correct=1, pnl_per_dollar=0.6, market_id="new")
    stats = ic.head_to_head_stats(factory, days=90)
    assert stats["n"] == 1  # only the new row is inside the window
