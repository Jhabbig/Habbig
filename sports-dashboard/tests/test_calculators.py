"""Tests for the bettor calculators (T?.? — bonus pro feature).

Pure-math functions with no state — verified against worked examples
that have known correct answers.
"""
import pytest
from fastapi.testclient import TestClient

import sports_dashboard as sd


def _client():
    return TestClient(sd.app)


# ── calc_odds_convert ──────────────────────────────────────────────────────

def test_decimal_to_others():
    """Decimal 2.00 = +100 American = 50% implied."""
    r = sd.calc_odds_convert(2.00, "decimal")
    assert r["decimal"] == 2.0
    assert r["american"] == 100.0
    assert abs(r["implied_pct"] - 50.0) < 0.01
    assert abs(r["implied_frac"] - 0.5) < 0.001


def test_american_negative():
    """-200 American = $200 to win $100 = 66.67% implied = 1.50 decimal."""
    r = sd.calc_odds_convert(-200, "american")
    assert abs(r["decimal"] - 1.5) < 0.001
    assert abs(r["implied_pct"] - 66.667) < 0.01


def test_american_positive():
    """+250 American = $100 to win $250 = 28.57% implied = 3.50 decimal."""
    r = sd.calc_odds_convert(250, "american")
    assert abs(r["decimal"] - 3.5) < 0.001
    assert abs(r["implied_pct"] - 28.571) < 0.01


def test_implied_pct_to_others():
    r = sd.calc_odds_convert(40.0, "implied_pct")
    assert abs(r["decimal"] - 2.5) < 0.001
    assert r["american"] == 150.0


def test_odds_convert_rejects_invalid():
    with pytest.raises(ValueError):
        sd.calc_odds_convert(1.0, "decimal")          # decimal must be > 1
    with pytest.raises(ValueError):
        sd.calc_odds_convert(0, "american")           # American can't be 0
    with pytest.raises(ValueError):
        sd.calc_odds_convert(150, "implied_pct")      # > 100
    with pytest.raises(ValueError):
        sd.calc_odds_convert(2.0, "trinary")          # unknown format


# ── calc_arbitrage ─────────────────────────────────────────────────────────

def test_arb_detects_negative_vig():
    """2.10 + 2.05 -> implied 47.62 + 48.78 = 96.40 → arb exists."""
    r = sd.calc_arbitrage(2.10, 2.05, total_stake=100.0)
    assert r["is_arbitrage"] is True
    assert r["profit"] > 0
    # Stakes sum to total
    assert abs(r["stake_a"] + r["stake_b"] - 100.0) < 0.02
    # Payouts equal either way (the stake_a/stake_b values are rounded to
    # cents in the response, so re-multiplying introduces small error;
    # accept ~1 cent tolerance)
    payout_a = r["stake_a"] * 2.10
    payout_b = r["stake_b"] * 2.05
    assert abs(payout_a - payout_b) < 0.05
    assert abs(r["payout"] - payout_a) < 0.05


def test_arb_no_edge_when_vig_high():
    """1.90 + 1.90 -> 52.63 + 52.63 = 105.26% → no arb (negative profit)."""
    r = sd.calc_arbitrage(1.90, 1.90, total_stake=100.0)
    assert r["is_arbitrage"] is False
    assert r["profit"] < 0


def test_arb_at_breakeven():
    """2.00 + 2.00 (no vig) → break even."""
    r = sd.calc_arbitrage(2.00, 2.00, total_stake=100.0)
    assert abs(r["profit"]) < 0.01


def test_arb_rejects_invalid():
    with pytest.raises(ValueError):
        sd.calc_arbitrage(1.0, 2.0)        # decimal odds must be > 1
    with pytest.raises(ValueError):
        sd.calc_arbitrage(2.0, 2.0, 0)     # stake must be > 0


# ── calc_hedge ─────────────────────────────────────────────────────────────

def test_hedge_equal_mode_balances_payouts():
    """Original $100 @ 3.00 = $300 payout. Hedge @ 2.00 → bet $150 →
    payout $300 either way. Net outlay $250, profit $50 either way."""
    r = sd.calc_hedge(3.00, 100.0, 2.00, mode="equal")
    assert abs(r["hedge_stake"] - 150.0) < 0.01
    assert abs(r["profit_if_original_wins"] - 50.0) < 0.01
    assert abs(r["profit_if_hedge_wins"] - 50.0) < 0.01
    assert abs(r["guaranteed_min_profit"] - 50.0) < 0.01


def test_hedge_breakeven_mode():
    """Hedge just enough to recover original stake. Original $100 @ 3.00.
    Hedge @ 2.00 → S' * (2-1) = 100 → S' = 100. If hedge wins, you get
    $200 back from $200 outlay = $0 profit. Original keeps upside."""
    r = sd.calc_hedge(3.00, 100.0, 2.00, mode="breakeven")
    assert abs(r["hedge_stake"] - 100.0) < 0.01
    assert abs(r["profit_if_hedge_wins"] - 0.0) < 0.01
    # If original wins: $300 - $200 outlay = $100 profit
    assert abs(r["profit_if_original_wins"] - 100.0) < 0.01


def test_hedge_no_hedge_mode():
    r = sd.calc_hedge(3.00, 100.0, 2.00, mode="no_hedge")
    assert r["hedge_stake"] == 0.0
    assert abs(r["profit_if_original_wins"] - 200.0) < 0.01  # $300 - $100 = $200 profit
    assert abs(r["profit_if_hedge_wins"] - -100.0) < 0.01     # lose original


def test_hedge_unknown_mode_raises():
    with pytest.raises(ValueError):
        sd.calc_hedge(2.0, 100.0, 2.0, mode="quantum")


# ── calc_promo_conversion ──────────────────────────────────────────────────

def test_promo_snr_freebet():
    """Standard $50 free bet (stake NOT returned) at 4.00 odds.
    Payout if win = $50 × (4-1) = $150. Hedge @ 1.35 → $150 / 1.35 = $111.11
    stake. Guaranteed cash = $150 - $111.11 = $38.89.
    Conversion ≈ $38.89 / $50 = 77.8%."""
    r = sd.calc_promo_conversion(50.0, 4.00, 1.35, stake_returned=False)
    assert abs(r["hedge_stake"] - 111.11) < 0.5
    assert abs(r["guaranteed_cash"] - 38.89) < 0.5
    assert 75.0 <= r["conversion_rate_pct"] <= 80.0


def test_promo_stake_returned():
    """$50 boosted-odds bet at 4.00 with own money. Payout = $50 × 4 = $200.
    Hedge @ 1.35 → $200 / 1.35 = $148.15. Cash = $200 - $148.15 = $51.85.
    Conversion = $51.85 / $50 = 103.7% (>100% because you're getting an edge,
    not a free bet — the conversion measures cash returned vs cash in)."""
    r = sd.calc_promo_conversion(50.0, 4.00, 1.35, stake_returned=True)
    assert abs(r["hedge_stake"] - 148.15) < 0.5
    assert abs(r["guaranteed_cash"] - 51.85) < 0.5
    assert r["conversion_rate_pct"] > 100


def test_promo_rejects_invalid():
    with pytest.raises(ValueError):
        sd.calc_promo_conversion(0, 2.0, 1.5)


# ── calc_devig ─────────────────────────────────────────────────────────────

def test_devig_basic():
    """55 + 50 = 105% → 5% vig. Fair = 52.38 + 47.62."""
    r = sd.calc_devig(55.0, 50.0)
    assert abs(r["vig_pct"] - 5.0) < 0.01
    assert abs(r["fair_prob_a_pct"] - 52.381) < 0.01
    assert abs(r["fair_prob_b_pct"] - 47.619) < 0.01
    # Fair sums to ~100
    assert abs(r["fair_prob_a_pct"] + r["fair_prob_b_pct"] - 100.0) < 0.01


def test_devig_no_vig():
    """50 + 50 → 0 vig, fair = inputs."""
    r = sd.calc_devig(50.0, 50.0)
    assert r["vig_pct"] == 0.0
    assert r["fair_prob_a_pct"] == 50.0


def test_devig_rejects_invalid():
    with pytest.raises(ValueError):
        sd.calc_devig(0, 50)
    with pytest.raises(ValueError):
        sd.calc_devig(50, 110)


# ── Endpoints (public — pure math, no auth) ─────────────────────────────────

def test_arb_endpoint_round_trip():
    r = _client().post("/api/calc/arbitrage", json={
        "decimal_odds_a": 2.10, "decimal_odds_b": 2.05, "total_stake": 100,
    })
    assert r.status_code == 200
    assert r.json()["is_arbitrage"] is True


def test_arb_endpoint_validates_missing_field():
    r = _client().post("/api/calc/arbitrage", json={"decimal_odds_a": 2.0})
    assert r.status_code == 400
    assert "decimal_odds_b" in r.json()["error"]


def test_arb_endpoint_validates_non_numeric():
    r = _client().post("/api/calc/arbitrage", json={
        "decimal_odds_a": "not-a-number", "decimal_odds_b": 2.0,
    })
    assert r.status_code == 400


def test_arb_endpoint_surfaces_math_errors():
    r = _client().post("/api/calc/arbitrage", json={
        "decimal_odds_a": 1.0, "decimal_odds_b": 2.0,
    })
    assert r.status_code == 400
    assert "decimal odds" in r.json()["error"]


def test_hedge_endpoint():
    r = _client().post("/api/calc/hedge", json={
        "original_decimal": 3.00, "original_stake": 100,
        "hedge_decimal": 2.00, "mode": "equal",
    })
    body = r.json()
    assert r.status_code == 200
    assert abs(body["hedge_stake"] - 150.0) < 0.01


def test_promo_endpoint():
    r = _client().post("/api/calc/promo-conversion", json={
        "free_bet_amount": 50, "free_bet_decimal": 4.0,
        "hedge_decimal": 1.35, "stake_returned": False,
    })
    assert r.status_code == 200
    assert 75 <= r.json()["conversion_rate_pct"] <= 80


def test_odds_convert_endpoint():
    r = _client().post("/api/calc/odds-convert", json={
        "value": 2.5, "format": "decimal",
    })
    assert r.status_code == 200
    assert r.json()["american"] == 150


def test_devig_endpoint():
    r = _client().post("/api/calc/devig", json={
        "prob_a_pct": 55, "prob_b_pct": 50,
    })
    assert r.status_code == 200
    assert abs(r.json()["vig_pct"] - 5.0) < 0.01


def test_calculators_page_is_public():
    r = _client().get("/calculators")
    assert r.status_code == 200
    assert "Calculators" in r.text
