"""Tests for Odds API quota tracking."""
from unittest.mock import Mock

import sports_dashboard as sd


def test_record_odds_quota_updates_state():
    sd._ODDS_QUOTA["remaining"] = None
    sd._ODDS_QUOTA["low_water_mark"] = None
    resp = Mock()
    resp.headers = {"x-requests-remaining": "450", "x-requests-used": "50"}
    sd._record_odds_quota(resp)
    assert sd._ODDS_QUOTA["remaining"] == 450
    assert sd._ODDS_QUOTA["used"] == 50
    assert sd._ODDS_QUOTA["low_water_mark"] == 450


def test_record_odds_quota_tracks_low_water_mark():
    sd._ODDS_QUOTA["low_water_mark"] = 100
    resp = Mock()
    resp.headers = {"x-requests-remaining": "50", "x-requests-used": "450"}
    sd._record_odds_quota(resp)
    assert sd._ODDS_QUOTA["low_water_mark"] == 50

    # Higher remaining should NOT raise the low-water mark
    resp.headers = {"x-requests-remaining": "200", "x-requests-used": "300"}
    sd._record_odds_quota(resp)
    assert sd._ODDS_QUOTA["low_water_mark"] == 50


def test_record_odds_quota_handles_missing_headers():
    resp = Mock()
    resp.headers = {}
    # Should not raise even when both headers are absent.
    result = sd._record_odds_quota(resp)
    assert result is None


def test_record_odds_quota_handles_non_numeric_headers():
    resp = Mock()
    resp.headers = {"x-requests-remaining": "not-a-number", "x-requests-used": ""}
    # Garbage headers should leave remaining unchanged but not crash.
    sd._ODDS_QUOTA["remaining"] = 999
    sd._record_odds_quota(resp)
    assert sd._ODDS_QUOTA["remaining"] is None  # garbage → reset to None


def test_odds_quota_remaining_returns_current():
    sd._ODDS_QUOTA["remaining"] = 123
    assert sd.odds_quota_remaining() == 123
