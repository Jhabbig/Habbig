"""
Integration test for the backtest CLI. Runs a subset of events and checks
that:
  * Every event in the fixture parses cleanly (no YAML regressions)
  * The full suite hits at least 30% of events (very loose sanity bound —
    the real calibration happens once we have 48h of live data)
  * The report renderer produces non-empty markdown with a per-event table
"""

from __future__ import annotations

from pathlib import Path

import pytest

import backtest as bt


pytestmark = pytest.mark.backtest


def test_fixture_has_at_least_30_events():
    events = bt._load_events()
    assert len(events) >= 30


def test_fixture_event_shape():
    for e in bt._load_events():
        assert e.id
        assert e.entity
        assert e.severity in {"minor", "moderate", "major", "watershed"}
        assert e.category
        assert e.target_window_iso
        assert isinstance(e.corpus_posts, list)
        assert len(e.corpus_posts) >= 2


def test_backtest_run_produces_replays():
    events = bt._load_events()
    replays = bt.run(events)
    assert len(replays) == len(events)
    assert all(r.event_id for r in replays)


def test_backtest_hit_rate_above_floor():
    """Loose sanity bound — not a calibration pass."""
    replays = bt.run(bt._load_events())
    hit = sum(1 for r in replays if r.fired)
    assert hit >= int(0.3 * len(replays)), (
        f"Only {hit}/{len(replays)} events fired; something is badly wrong"
    )


def test_render_report_not_empty():
    replays = bt.run(bt._load_events())
    md = bt._render_report(replays, daily_rate=7.5, corpus_per_day=40)
    assert "# Annoyance Dashboard — Backtest Report" in md
    assert "Per-event results" in md
    assert "| " in md  # at least one table row
