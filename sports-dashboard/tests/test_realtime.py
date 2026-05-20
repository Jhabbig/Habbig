"""Tests for the live Polymarket WS layer and adaptive poll-interval policy.

The WS subscriber itself can't run in CI (no outbound network in the
sandbox), so these tests exercise the pure-function pieces:
- subscription set collection
- live-price application to comparisons (the recompute path)
- poll-interval decision matrix
"""
import time
from datetime import datetime, timedelta, timezone

import sports_dashboard as sd


# ── _collect_poly_token_ids ─────────────────────────────────────────────────

def test_collect_token_ids_empty():
    assert sd._collect_poly_token_ids([]) == set()


def test_collect_token_ids_pulls_from_all_outcomes():
    comps = [
        {"outcomes": [{"poly_token_id": "a"}, {"poly_token_id": "b"}]},
        {"outcomes": [{"poly_token_id": "b"}, {"poly_token_id": None}]},
        {"outcomes": [{"poly_token_id": "c"}]},
    ]
    assert sd._collect_poly_token_ids(comps) == {"a", "b", "c"}


def test_collect_token_ids_ignores_missing_field():
    comps = [{"outcomes": [{"sharp_prob": 50.0}]}]  # no poly_token_id key
    assert sd._collect_poly_token_ids(comps) == set()


# ── _apply_live_prices_to_comparisons ───────────────────────────────────────

def test_apply_live_prices_overrides_poly_prob():
    sd._LIVE_POLY_PRICES.clear()
    sd._LIVE_POLY_PRICES["tok-1"] = {"price": 0.45, "ts": time.time()}
    comps = [{
        "outcomes": [{
            "poly_token_id": "tok-1",
            "sharp_prob": 55.0,
            "poly_prob": 40.0,
            "divergence": 15.0,
            "divergence_raw": 15.0,
            "is_signal": True,
            "sharp_consensus_ok": True,
            "liquidity_ok": True,
            "not_stale": True,
        }],
    }]
    n = sd._apply_live_prices_to_comparisons(comps)
    assert n == 1
    oc = comps[0]["outcomes"][0]
    assert oc["poly_prob"] == 45.0
    assert oc["poly_price"] == 0.45
    # 55 - 45 = 10 pp divergence
    assert oc["divergence_raw"] == 10.0
    # is_signal still True because divergence (10) >= threshold (5) and all gates pass
    assert oc["is_signal"] is True


def test_apply_live_prices_skips_stale_prices():
    sd._LIVE_POLY_PRICES.clear()
    # 5 minutes old — older than PM_WS_PRICE_FRESH_SECONDS (90s)
    sd._LIVE_POLY_PRICES["tok-stale"] = {"price": 0.45, "ts": time.time() - 300}
    comps = [{
        "outcomes": [{
            "poly_token_id": "tok-stale",
            "sharp_prob": 55.0, "poly_prob": 40.0,
            "divergence": 15.0, "divergence_raw": 15.0,
            "sharp_consensus_ok": True, "liquidity_ok": True, "not_stale": True,
        }],
    }]
    n = sd._apply_live_prices_to_comparisons(comps)
    assert n == 0
    # Poly_prob untouched
    assert comps[0]["outcomes"][0]["poly_prob"] == 40.0


def test_apply_live_prices_no_match_when_token_unknown():
    sd._LIVE_POLY_PRICES.clear()
    sd._LIVE_POLY_PRICES["other"] = {"price": 0.5, "ts": time.time()}
    comps = [{"outcomes": [{"poly_token_id": "tok-1", "sharp_prob": 55.0,
                             "poly_prob": 40.0, "divergence": 15.0,
                             "divergence_raw": 15.0,
                             "sharp_consensus_ok": True, "liquidity_ok": True,
                             "not_stale": True}]}]
    n = sd._apply_live_prices_to_comparisons(comps)
    assert n == 0


def test_apply_live_prices_flips_signal_off_when_divergence_collapses():
    """Live price brings poly into line with sharp -> signal turns off."""
    sd._LIVE_POLY_PRICES.clear()
    sd._LIVE_POLY_PRICES["tok-1"] = {"price": 0.54, "ts": time.time()}
    comps = [{
        "outcomes": [{
            "poly_token_id": "tok-1",
            "sharp_prob": 55.0, "poly_prob": 40.0,
            "divergence": 15.0, "divergence_raw": 15.0,
            "is_signal": True,
            "sharp_consensus_ok": True, "liquidity_ok": True, "not_stale": True,
        }],
    }]
    sd._apply_live_prices_to_comparisons(comps)
    # 55 - 54 = 1 pp — below threshold 5
    assert comps[0]["outcomes"][0]["divergence_raw"] == 1.0
    assert comps[0]["outcomes"][0]["is_signal"] is False


def test_apply_live_prices_recomputes_has_signal_at_comparison_level():
    sd._LIVE_POLY_PRICES.clear()
    sd._LIVE_POLY_PRICES["a"] = {"price": 0.54, "ts": time.time()}
    sd._LIVE_POLY_PRICES["b"] = {"price": 0.40, "ts": time.time()}
    comp = {
        "outcomes": [
            {"poly_token_id": "a", "sharp_prob": 55.0, "poly_prob": 40.0,
             "divergence": 15.0, "divergence_raw": 15.0, "is_signal": True,
             "sharp_consensus_ok": True, "liquidity_ok": True, "not_stale": True},
            {"poly_token_id": "b", "sharp_prob": 50.0, "poly_prob": 50.0,
             "divergence": 0.0, "divergence_raw": 0.0, "is_signal": False,
             "sharp_consensus_ok": True, "liquidity_ok": True, "not_stale": True},
        ],
        "has_signal": True, "max_divergence": 15.0,
    }
    sd._apply_live_prices_to_comparisons([comp])
    # Outcome a: poly 40 -> 54 collapses divergence to 1pp (no signal)
    # Outcome b: poly 50 -> 40, divergence becomes +10pp (now a signal!)
    assert comp["has_signal"] is True
    assert comp["max_divergence"] == 10.0


# ── _update_pm_ws_subscriptions ─────────────────────────────────────────────

def test_update_subscriptions_sets_desired_tokens():
    sd._PM_WS_DESIRED_TOKENS = set()
    comps = [{"outcomes": [{"poly_token_id": "x"}, {"poly_token_id": "y"}]}]
    sd._update_pm_ws_subscriptions(comps)
    assert sd._PM_WS_DESIRED_TOKENS == {"x", "y"}


def test_update_subscriptions_respects_cap():
    sd._PM_WS_DESIRED_TOKENS = set()
    comps = [{"outcomes": [{"poly_token_id": f"t{i}"} for i in range(500)]}]
    sd._update_pm_ws_subscriptions(comps)
    assert len(sd._PM_WS_DESIRED_TOKENS) == sd.PM_WS_MAX_SUBSCRIPTIONS


# ── Adaptive poll interval ──────────────────────────────────────────────────

def _comp_with_kickoff(hours_from_now: float) -> dict:
    dt = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    return {"commence_time": dt.isoformat(), "outcomes": []}


def test_interval_idle_when_no_comparisons():
    assert sd._compute_poll_interval([], None) == sd.POLL_INTERVAL_IDLE


def test_interval_pre_game_when_kickoff_imminent():
    comps = [_comp_with_kickoff(0.25)]  # 15 min away
    assert sd._compute_poll_interval(comps, None) == sd.POLL_INTERVAL_PRE_GAME


def test_interval_soon_when_kickoff_a_few_hours_out():
    comps = [_comp_with_kickoff(3)]
    assert sd._compute_poll_interval(comps, None) == sd.POLL_INTERVAL_SOON


def test_interval_today_when_kickoff_is_later_today():
    comps = [_comp_with_kickoff(18)]
    assert sd._compute_poll_interval(comps, None) == sd.POLL_INTERVAL_TODAY


def test_interval_idle_when_kickoff_far_in_future():
    comps = [_comp_with_kickoff(48)]
    assert sd._compute_poll_interval(comps, None) == sd.POLL_INTERVAL_IDLE


def test_interval_picks_nearest_game():
    comps = [
        _comp_with_kickoff(48),
        _comp_with_kickoff(2),   # this one wins
        _comp_with_kickoff(20),
    ]
    assert sd._compute_poll_interval(comps, None) == sd.POLL_INTERVAL_SOON


def test_interval_quota_floor_dominates_when_quota_low():
    """Even with a game 15 min away, low quota forces the loop to slow down."""
    comps = [_comp_with_kickoff(0.25)]
    assert sd._compute_poll_interval(comps, remaining=20) == 1800


def test_interval_quota_floor_softer_at_higher_remaining():
    comps = [_comp_with_kickoff(0.25)]
    # 200 remaining -> 600s floor, but pre-game is 15s -> 600 wins
    assert sd._compute_poll_interval(comps, remaining=200) == 600


def test_interval_quota_does_not_force_slowdown_when_healthy():
    comps = [_comp_with_kickoff(0.25)]
    assert sd._compute_poll_interval(comps, remaining=1000) == sd.POLL_INTERVAL_PRE_GAME


def test_interval_ignores_unparseable_commence_time():
    comps = [{"commence_time": "garbage", "outcomes": []}]
    assert sd._compute_poll_interval(comps, None) == sd.POLL_INTERVAL_IDLE


def test_interval_live_game_uses_pre_game_cadence():
    """A game already started (negative hours) should still be at fast cadence."""
    comps = [_comp_with_kickoff(-0.5)]  # 30 min ago
    assert sd._compute_poll_interval(comps, None) == sd.POLL_INTERVAL_PRE_GAME
