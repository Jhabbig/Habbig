"""Tests for team-name normalization, alias coverage, and the
fuzzy matching engine's near-reject diagnostic buffer.

These tests do not hit any external API. They exercise the pure-function
parts of the matching pipeline with synthetic inputs.
"""
from datetime import datetime, timedelta, timezone

import sports_dashboard as sd


# ── normalize_name + alias coverage ─────────────────────────────────────────

def test_normalize_lowercases_and_strips():
    assert sd.normalize_name("  Manchester City  ") == "manchester city"


def test_normalize_resolves_epl_aliases():
    assert sd.normalize_name("Man Utd") == "manchester united"
    assert sd.normalize_name("Spurs") == "tottenham hotspur"
    assert sd.normalize_name("Wolves") == "wolverhampton wanderers"


def test_normalize_resolves_nba_aliases():
    assert sd.normalize_name("Sixers") == "philadelphia 76ers"
    assert sd.normalize_name("OKC") == "oklahoma city thunder"
    assert sd.normalize_name("GSW") == "golden state warriors"


def test_normalize_resolves_nfl_aliases():
    assert sd.normalize_name("Niners") == "san francisco 49ers"
    assert sd.normalize_name("Chiefs") == "kansas city chiefs"


def test_normalize_resolves_mlb_aliases():
    assert sd.normalize_name("Yankees") == "new york yankees"
    assert sd.normalize_name("Dodgers") == "los angeles dodgers"


def test_normalize_resolves_nhl_aliases():
    assert sd.normalize_name("Leafs") == "toronto maple leafs"
    assert sd.normalize_name("Habs") == "montreal canadiens"


def test_normalize_unknown_passthrough():
    """Unknown names should pass through lowercased, never raise."""
    assert sd.normalize_name("Some FC") == "some fc"


def test_alias_table_size():
    """Lock in coverage so we don't regress when adding new sports."""
    assert len(sd.TEAM_ALIASES) >= 100


# ── _parse_iso_utc ──────────────────────────────────────────────────────────

def test_parse_iso_utc_with_z():
    dt = sd._parse_iso_utc("2026-01-15T18:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_utc_with_offset():
    dt = sd._parse_iso_utc("2026-01-15T18:00:00+00:00")
    assert dt is not None


def test_parse_iso_utc_invalid_returns_none():
    assert sd._parse_iso_utc("not a date") is None
    assert sd._parse_iso_utc("") is None


# ── Near-reject diagnostic buffer ───────────────────────────────────────────

def test_near_reject_logs_to_buffer():
    sd._NEAR_REJECTS.clear()
    event = {"home_team": "Foo", "away_team": "Bar", "commence_time": "2026-01-15T18:00:00Z"}
    pm = {"market_question": "Q", "event_title": "ET", "end_date": "2026-01-15T20:00:00Z"}
    sd._log_near_reject(event, pm, home_score=68, away_score=58, reason="team_score_below_70")
    assert len(sd._NEAR_REJECTS) == 1
    rec = sd._NEAR_REJECTS[0]
    assert rec["event"] == "Foo vs Bar"
    assert rec["home_score"] == 68
    assert rec["reason"] == "team_score_below_70"


def test_near_reject_buffer_is_capped():
    sd._NEAR_REJECTS.clear()
    event = {"home_team": "X", "away_team": "Y", "commence_time": ""}
    pm = {"market_question": "", "event_title": "", "end_date": ""}
    for _ in range(sd._NEAR_REJECTS_MAX + 50):
        sd._log_near_reject(event, pm, 60, 60, "x")
    assert len(sd._NEAR_REJECTS) == sd._NEAR_REJECTS_MAX


# ── match_and_compare time-window check ─────────────────────────────────────

def _make_event(home: str, away: str, commence: str):
    """Build a minimal odds event the matcher accepts."""
    return {
        "home_team": home,
        "away_team": away,
        "commence_time": commence,
        "sharp_outcomes": {home: {"implied_prob": 60.0}, away: {"implied_prob": 40.0}},
        "consensus_probs": {home: 60.0, away: 40.0},
        "bookmakers": {},
        "num_bookmakers": 0,
        "sharp_book": "test",
    }


def _make_poly(question: str, end_date: str, outcomes: dict | None = None):
    """Build a minimal Polymarket market the matcher accepts."""
    return {
        "event_id": question,
        "event_title": question,
        "market_question": question,
        "group_title": "",
        "slug": "x",
        "condition_id": "x",
        "outcomes": outcomes or {"Yes": {"implied_prob": 50.0}, "No": {"implied_prob": 50.0}},
        "volume": 1000.0,
        "liquidity": 500.0,
        "liquidity_clob": 0.0,
        "best_bid": 0.0,
        "best_ask": 0.0,
        "spread": 0.01,
        "one_day_change": 0.0,
        "one_week_change": 0.0,
        "last_trade_price": 0.5,
        "tags": [],
        "start_date": "",
        "end_date": end_date,
    }


def test_match_rejects_when_poly_resolves_far_after_event():
    """Polymarket end_date 30 days after the bookmaker event is rejected."""
    commence = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)
    far_after = commence + timedelta(days=30)
    event = _make_event("Lakers", "Warriors", commence.isoformat())
    pm = _make_poly("Lakers vs Warriors", far_after.isoformat())
    result = sd.match_and_compare([event], [pm])
    assert result == []


def test_match_accepts_within_window():
    """Polymarket end_date within window: matches and returns a comparison.

    Polymarket questions typically use full team names ("Los Angeles Lakers"),
    so we match the post-alias normalization. Bookmakers ("Lakers") get
    expanded by `normalize_name` before fuzzy matching.
    """
    commence = datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)
    within = commence + timedelta(hours=4)
    event = _make_event("Lakers", "Warriors", commence.isoformat())
    pm = _make_poly(
        "Will the Los Angeles Lakers beat the Golden State Warriors?",
        within.isoformat(),
        outcomes={"Yes": {"implied_prob": 55.0}, "No": {"implied_prob": 45.0}},
    )
    result = sd.match_and_compare([event], [pm])
    assert len(result) == 1
    assert result[0]["home_team"] == "Lakers"
