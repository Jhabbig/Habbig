"""Tests for political news ingest + market-reaction measurement.

Run:
    cd backend && python3 test_news.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from news import (
    REACTION_THRESHOLD,
    compute_market_reaction,
    lag_curve,
    tag_article,
)


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# ── Article tagging ──────────────────────────────────────────────────

def test_tag_article_senate_full_name():
    t = tag_article("Senate race in Pennsylvania heats up")
    if t["race_key"] != "senate_PA":
        _fail(f"expected senate_PA, got {t}")
    print("PASS tag_article_senate_full_name")


def test_tag_article_governor_via_newsom():
    t = tag_article("Newsom signs bill amid gubernatorial speculation")
    if t["race_key"] != "governor_CA":
        _fail(f"expected governor_CA, got {t}")
    print("PASS tag_article_governor_via_newsom")


def test_tag_article_dc_not_washington_state():
    """Washington D.C. mentions should not tag as Washington state races."""
    t = tag_article("Senate hearings in Washington D.C. focus on the FTC")
    if t["state"] == "WA":
        _fail(f"DC headline shouldn't pick WA: {t}")
    print("PASS tag_article_dc_not_washington_state")


def test_tag_article_no_signal():
    t = tag_article("Bitcoin hits a new all-time high")
    if t["race_key"] is not None:
        _fail(f"non-political headline shouldn't tag: {t}")
    print("PASS tag_article_no_signal")


def test_tag_article_requires_both_office_and_state():
    """A pure state mention with no office should not yield a race_key."""
    t = tag_article("Texas wildfires force evacuations")
    if t["race_key"] is not None:
        _fail(f"state-only headline shouldn't pick a race: {t}")
    print("PASS tag_article_requires_both_office_and_state")


# ── Market reaction ──────────────────────────────────────────────────

def test_reaction_detects_lag():
    now = datetime(2026, 5, 6, 14, 0, 0, tzinfo=timezone.utc)
    before = [{"timestamp": "2026-05-06T13:55:00+00:00", "prices": {"Yes": 0.40, "No": 0.60}}]
    after = [
        {"timestamp": "2026-05-06T14:05:00+00:00", "prices": {"Yes": 0.42, "No": 0.58}},
        {"timestamp": "2026-05-06T14:10:00+00:00", "prices": {"Yes": 0.46, "No": 0.54}},
    ]
    r = compute_market_reaction(snapshots_before=before, snapshots_after=after, news_published_at=now)
    if r is None:
        _fail("reaction should be measurable")
    if r["delta_pp"] < 5:
        _fail(f"delta_pp too small: {r}")
    if r["lag_seconds"] is None or r["lag_seconds"] not in (300, 600):
        _fail(f"lag_seconds wrong: {r}")
    print("PASS reaction_detects_lag")


def test_reaction_handles_no_movement():
    """When prices don't move beyond the threshold, lag_seconds should be None."""
    now = datetime(2026, 5, 6, 14, 0, 0, tzinfo=timezone.utc)
    before = [{"timestamp": "2026-05-06T13:55:00+00:00", "prices": {"Yes": 0.50}}]
    after = [
        {"timestamp": "2026-05-06T14:05:00+00:00", "prices": {"Yes": 0.501}},
        {"timestamp": "2026-05-06T14:30:00+00:00", "prices": {"Yes": 0.499}},
    ]
    r = compute_market_reaction(snapshots_before=before, snapshots_after=after, news_published_at=now)
    if r is None:
        _fail("flat reaction should still compute, just with no lag")
    if r["lag_seconds"] is not None:
        _fail(f"flat reaction shouldn't have a lag: {r}")
    print("PASS reaction_handles_no_movement")


def test_reaction_returns_none_without_snapshots():
    now = datetime(2026, 5, 6, 14, 0, 0, tzinfo=timezone.utc)
    if compute_market_reaction(snapshots_before=[], snapshots_after=[], news_published_at=now) is not None:
        _fail("empty inputs should return None")
    print("PASS reaction_returns_none_without_snapshots")


# ── Lag curve ────────────────────────────────────────────────────────

def test_lag_curve_filters_small_moves():
    reactions = [
        {"source": "polymarket", "delta_pp": 5.0, "lag_seconds": 300},
        {"source": "polymarket", "delta_pp": 3.0, "lag_seconds": 600},
        {"source": "polymarket", "delta_pp": 0.5, "lag_seconds": 50},  # below threshold
        {"source": "kalshi",     "delta_pp": 4.0, "lag_seconds": 900},
    ]
    c = lag_curve(reactions, min_delta_pp=1.0)
    if c["by_source"]["polymarket"]["n"] != 2:
        _fail(f"small moves should be filtered: {c}")
    if c["by_source"]["polymarket"]["median_lag_s"] != 450:
        _fail(f"median wrong: {c}")
    print("PASS lag_curve_filters_small_moves")


def test_lag_curve_skips_null_lag():
    """Reactions with no lag (no material move) should not contribute to the curve."""
    reactions = [
        {"source": "polymarket", "delta_pp": 5.0, "lag_seconds": None},
        {"source": "polymarket", "delta_pp": 4.0, "lag_seconds": 600},
    ]
    c = lag_curve(reactions, min_delta_pp=1.0)
    if c["by_source"]["polymarket"]["n"] != 1:
        _fail(f"null-lag should be excluded: {c}")
    print("PASS lag_curve_skips_null_lag")


if __name__ == "__main__":
    test_tag_article_senate_full_name()
    test_tag_article_governor_via_newsom()
    test_tag_article_dc_not_washington_state()
    test_tag_article_no_signal()
    test_tag_article_requires_both_office_and_state()
    test_reaction_detects_lag()
    test_reaction_handles_no_movement()
    test_reaction_returns_none_without_snapshots()
    test_lag_curve_filters_small_moves()
    test_lag_curve_skips_null_lag()
    print("\nAll news tests passed.")
