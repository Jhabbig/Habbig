"""Tests for the rule-based alert engine."""
from datetime import datetime, timezone

import sports_dashboard as sd


def _signal(home="Lakers", away="Warriors", market_type="h2h",
            max_div=8.0, poly_volume=10000.0, time_to_event_hours=2.0,
            outcomes_pass_gates=True, has_is_signal=True):
    """Build a comparison-shaped signal dict with one passing outcome."""
    oc = {
        "outcome_name": "Lakers",
        "divergence_pct": max_div,
        "is_signal": has_is_signal,
        "sharp_consensus_ok": outcomes_pass_gates,
        "not_stale": outcomes_pass_gates,
        "liquidity_ok": outcomes_pass_gates,
    }
    return {
        "home_team": home,
        "away_team": away,
        "market_type": market_type,
        "max_divergence": max_div,
        "poly_volume": poly_volume,
        "time_to_event_hours": time_to_event_hours,
        "outcomes": [oc],
    }


def _rule(**kwargs):
    """Build a rule dict matching the sports_alert_rules row shape."""
    base = {
        "id": 1,
        "user_id": "u1",
        "name": "test",
        "enabled": 1,
        "sports": "[]",
        "market_types": "[]",
        "min_divergence_pp": 5.0,
        "min_volume": None,
        "max_time_to_event_hours": None,
        "require_sharp_consensus": 1,
        "require_not_stale": 1,
        "require_liquidity_ok": 1,
        "channel": "telegram",
        "quiet_hours_start": None,
        "quiet_hours_end": None,
        "cooldown_secs": 300,
        "last_fired_at": "",
    }
    base.update(kwargs)
    return base


# ── _signal_matches_rule ────────────────────────────────────────────────────

def test_rule_matches_baseline():
    assert sd._signal_matches_rule(_signal(), "basketball_nba", _rule()) is True


def test_rule_filters_by_sport():
    r = _rule(sports='["americanfootball_nfl"]')
    assert sd._signal_matches_rule(_signal(), "basketball_nba", r) is False
    assert sd._signal_matches_rule(_signal(), "americanfootball_nfl", r) is True


def test_rule_filters_by_market_type():
    r = _rule(market_types='["spreads", "totals"]')
    assert sd._signal_matches_rule(_signal(market_type="h2h"), "nba", r) is False
    assert sd._signal_matches_rule(_signal(market_type="spreads"), "nba", r) is True


def test_rule_filters_by_min_divergence():
    r = _rule(min_divergence_pp=10.0)
    assert sd._signal_matches_rule(_signal(max_div=8.0), "nba", r) is False
    assert sd._signal_matches_rule(_signal(max_div=12.0), "nba", r) is True


def test_rule_filters_by_min_volume():
    r = _rule(min_volume=5000.0)
    assert sd._signal_matches_rule(_signal(poly_volume=2000), "nba", r) is False
    assert sd._signal_matches_rule(_signal(poly_volume=10000), "nba", r) is True


def test_rule_filters_by_max_time_to_event():
    r = _rule(max_time_to_event_hours=4.0)
    assert sd._signal_matches_rule(_signal(time_to_event_hours=10), "nba", r) is False
    assert sd._signal_matches_rule(_signal(time_to_event_hours=2), "nba", r) is True


def test_rule_filters_by_quality_gates():
    """When gates are required, an outcome that fails them must not pass."""
    r = _rule(require_sharp_consensus=1, require_not_stale=1, require_liquidity_ok=1)
    assert sd._signal_matches_rule(
        _signal(outcomes_pass_gates=False), "nba", r) is False
    # Same signal but with gates disabled in the rule -> passes
    r2 = _rule(require_sharp_consensus=0, require_not_stale=0, require_liquidity_ok=0)
    assert sd._signal_matches_rule(
        _signal(outcomes_pass_gates=False), "nba", r2) is True


def test_rule_filters_by_is_signal_per_outcome():
    """At least one outcome must have is_signal=True."""
    s = _signal(has_is_signal=False)
    assert sd._signal_matches_rule(s, "nba", _rule()) is False


def test_rule_no_match_when_no_commence_time_and_window_set():
    """time_to_event_hours=None should fail max_time_to_event filter."""
    s = _signal(time_to_event_hours=None)
    r = _rule(max_time_to_event_hours=24.0)
    assert sd._signal_matches_rule(s, "nba", r) is False


# ── _rule_quiet_hours_active ────────────────────────────────────────────────

def _at(hour_utc: int) -> datetime:
    return datetime(2026, 1, 15, hour_utc, 0, tzinfo=timezone.utc)


def test_quiet_hours_simple_window():
    r = _rule(quiet_hours_start=22, quiet_hours_end=6)
    assert sd._rule_quiet_hours_active(r, _at(23)) is True
    assert sd._rule_quiet_hours_active(r, _at(2)) is True
    assert sd._rule_quiet_hours_active(r, _at(14)) is False


def test_quiet_hours_daytime_window():
    """Quiet during the day, alerts at night — uncommon but supported."""
    r = _rule(quiet_hours_start=9, quiet_hours_end=17)
    assert sd._rule_quiet_hours_active(r, _at(12)) is True
    assert sd._rule_quiet_hours_active(r, _at(20)) is False


def test_quiet_hours_disabled_when_unset():
    assert sd._rule_quiet_hours_active(_rule(), _at(3)) is False


# ── _validate_rule_body ─────────────────────────────────────────────────────

def test_validate_rule_body_minimal():
    fields, err = sd._validate_rule_body({})
    assert err is None
    assert fields == {}


def test_validate_rule_body_full():
    fields, err = sd._validate_rule_body({
        "name": "NBA late-window",
        "enabled": True,
        "sports": ["basketball_nba"],
        "market_types": ["h2h", "totals"],
        "min_divergence_pp": 7.5,
        "min_volume": 5000,
        "max_time_to_event_hours": 4,
        "require_sharp_consensus": True,
        "channel": "both",
        "quiet_hours_start": 22,
        "quiet_hours_end": 6,
        "cooldown_secs": 600,
    })
    assert err is None
    assert fields["name"] == "NBA late-window"
    assert fields["enabled"] == 1
    assert fields["sports"] == '["basketball_nba"]'
    assert fields["market_types"] == '["h2h", "totals"]'
    assert fields["min_divergence_pp"] == 7.5
    assert fields["channel"] == "both"


def test_validate_rule_body_rejects_bad_channel():
    fields, err = sd._validate_rule_body({"channel": "carrier-pigeon"})
    assert fields is None
    assert "channel" in err


def test_validate_rule_body_rejects_bad_market_type():
    fields, err = sd._validate_rule_body({"market_types": ["h2h", "moneyline-plus"]})
    assert fields is None
    assert "market_types" in err


def test_validate_rule_body_rejects_invalid_quiet_hour():
    fields, err = sd._validate_rule_body({"quiet_hours_start": 25})
    assert fields is None


def test_validate_rule_body_clears_optional_field_on_empty():
    """Sending null/empty for min_volume should null it out (partial update)."""
    fields, err = sd._validate_rule_body({"min_volume": None})
    assert err is None
    assert fields["min_volume"] is None


def test_validate_rule_body_name_is_capped():
    """Names over 80 chars get truncated rather than rejected."""
    fields, _ = sd._validate_rule_body({"name": "x" * 200})
    assert len(fields["name"]) == 80


def test_validate_rule_body_invalid_min_divergence():
    fields, err = sd._validate_rule_body({"min_divergence_pp": -1})
    assert fields is None
