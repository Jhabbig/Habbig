"""Tests for intraday conditional-probability narrowing.

These pin down the headline behavior: when running max already meets a
threshold the conditional probability snaps to ~1.0, when it's far
below and we're past peak it snaps to ~0.0, and the in-between cases
are sharper than the unconditional Gaussian.
"""

import pytest

from weather_intraday import (
    conditional_max_distribution,
    conditional_probability,
    conditional_probability_above,
    conditional_probability_below,
    remaining_hourly_max,
)


# ─── remaining_hourly_max ─────────────────────────────────────────────────────

def test_remaining_hourly_max_basic():
    hourly = [60, 62, 65, 70, 75, 78, 80, 82, 84, 85, 84, 82,
              80, 78, 75, 72, 70, 68, 66, 64, 62, 60, 58, 56]
    out = remaining_hourly_max(hourly, hours_elapsed=10)
    # Hours 10..23 → max should be 84
    assert out["max"] == 84
    assert out["n"] == 14


def test_remaining_hourly_max_past_end():
    hourly = [60, 65, 70]
    out = remaining_hourly_max(hourly, hours_elapsed=10)
    assert out["max"] is None
    assert out["n"] == 0


def test_remaining_hourly_max_handles_none_entries():
    hourly = [60, None, 70, 75, None, 80]
    out = remaining_hourly_max(hourly, hours_elapsed=2)
    assert out["max"] == 80


# ─── conditional_max_distribution ─────────────────────────────────────────────

def test_conditional_dist_running_max_already_dominates():
    """At 4pm with running_max=85 and only 60s forecasted for the rest of
    the day, the running max is the floor and very nearly the final."""
    hourly = [60, 62, 65, 70, 75, 80, 82, 85, 84, 80, 75, 70,
              65, 60, 58, 55, 53, 50, 48, 46, 45, 44, 43, 42]
    dist = conditional_max_distribution(running_max_f=85.0,
                                        hourly_temps=hourly,
                                        hours_elapsed=14,
                                        station_residual_std=2.5)
    assert dist is not None
    assert dist["mean"] == 85.0
    assert dist["hard_floor"] == 85.0
    assert dist["std"] <= 1.0


def test_conditional_dist_pre_peak_uses_hourly_forecast():
    """At 9am, peak is forecast at 1pm — distribution should center near
    the forecast peak, not the running observation."""
    hourly = [60, 62, 65, 68, 70, 72, 74, 78, 82, 86, 88, 85,
              82, 80, 78, 75, 72, 70, 68, 66, 64, 62, 60, 58]
    dist = conditional_max_distribution(running_max_f=68.0,
                                        hourly_temps=hourly,
                                        hours_elapsed=9,
                                        station_residual_std=2.5)
    assert dist is not None
    # Peak is 88 (hour 10), so mean should be ≥ 88
    assert dist["mean"] >= 88.0


def test_conditional_dist_past_end_returns_observed():
    """End of day: only the running max is left."""
    dist = conditional_max_distribution(running_max_f=78.0,
                                        hourly_temps=[60, 62, 65],
                                        hours_elapsed=24,
                                        station_residual_std=2.5)
    assert dist is not None
    assert dist["source"] == "post_peak_observed"
    assert dist["mean"] == 78.0


def test_conditional_dist_no_inputs_returns_none():
    assert conditional_max_distribution(None, None, 12) is None


# ─── conditional_probability_above / _below ──────────────────────────────────

def test_probability_snaps_to_one_when_floor_meets_threshold():
    """If running max already exceeds the threshold, P(YES) ≈ 1.0."""
    dist = {"mean": 80, "std": 2.0, "hard_floor": 80, "source": "test"}
    assert conditional_probability_above(75, dist) == 0.99
    assert conditional_probability_above(80, dist) == 0.99
    assert conditional_probability_above(85, dist) is not None
    assert conditional_probability_above(85, dist) < 0.5


def test_probability_below_when_floor_passes_threshold():
    """If running max already exceeds the below-threshold, P(below) ≈ 0."""
    dist = {"mean": 85, "std": 2.0, "hard_floor": 85, "source": "test"}
    assert conditional_probability_below(80, dist) == 0.01


def test_intraday_sharper_than_unconditional():
    """Hold the unconditional Gaussian roughly equal to coin-flip; show
    that conditioning on the running max moves it well past 0.5."""
    # Forecast says daily max ~ 76°F with std 4 — P(>75) is just over 0.5
    from scipy.stats import norm
    p_unconditional = 1.0 - norm.cdf(75, loc=76, scale=4)
    assert 0.5 < p_unconditional < 0.7

    # Now we observe running_max = 78 at 3pm with low remaining-day forecast
    hourly = [60, 62, 65, 68, 70, 72, 75, 78, 75, 72, 68,
              65, 62, 60, 58, 55, 53, 50, 48, 46, 45, 44, 43, 42]
    dist = conditional_max_distribution(running_max_f=78.0,
                                        hourly_temps=hourly,
                                        hours_elapsed=15,
                                        station_residual_std=2.5)
    p_conditional = conditional_probability_above(75, dist)
    assert p_conditional > 0.95  # Snaps to certainty


# ─── conditional_probability (threshold_info shape) ──────────────────────────

def test_conditional_probability_above_via_threshold_info():
    info = {"threshold": 75, "is_over": True, "unit": "F"}
    dist = {"mean": 78, "std": 2.0, "hard_floor": 76, "source": "test"}
    p = conditional_probability(info, dist)
    assert p == 0.99


def test_conditional_probability_below_via_threshold_info():
    info = {"threshold": 70, "is_over": False, "unit": "F"}
    dist = {"mean": 65, "std": 2.0, "hard_floor": 60, "source": "test"}
    p = conditional_probability(info, dist)
    assert p is not None
    assert p > 0.9  # Mean is well below threshold and floor is fine


def test_conditional_probability_handles_celsius():
    # 25°C threshold == 77°F
    info = {"threshold": 25.0, "is_over": True, "unit": "C"}
    dist = {"mean": 80, "std": 1.0, "hard_floor": 79, "source": "test"}
    p = conditional_probability(info, dist)
    # Floor (79) > threshold (77) so probability ≈ 1
    assert p == 0.99


def test_conditional_probability_range_with_floor_inside():
    """Range market 70-75°F with running_max already 73."""
    info = {"temp_lower": 70.0, "temp_upper": 75.0, "unit": "F"}
    dist = {"mean": 73.5, "std": 1.0, "hard_floor": 73, "source": "test"}
    p = conditional_probability(info, dist)
    assert p is not None
    # We're already in range; question is whether it'll stay below 75
    assert 0.7 < p < 0.99


def test_conditional_probability_range_with_floor_above():
    """Range market 70-75°F but running_max already 78 → impossible."""
    info = {"temp_lower": 70.0, "temp_upper": 75.0, "unit": "F"}
    dist = {"mean": 78, "std": 1.0, "hard_floor": 78, "source": "test"}
    p = conditional_probability(info, dist)
    assert p == 0.01


def test_conditional_probability_returns_none_on_garbage():
    assert conditional_probability(None, None) is None
    assert conditional_probability({}, None) is None
    assert conditional_probability({"threshold": 70, "is_over": True}, None) is None
