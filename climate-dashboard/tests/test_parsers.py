"""Snapshot tests for upstream-data parsers and the prediction models.

These run pure-Python — no network, no flask. The fixture files are tiny
clipped samples of the real upstream formats. They're enough to lock in the
schema each parser produces and catch upstream format drift.

Run with: ``pytest`` from climate-dashboard/, or ``python3 -m pytest tests``.
"""
from __future__ import annotations

import os
from pathlib import Path

from app import math_utils
from app.fetchers import co2 as co2_src
from app.fetchers import gistemp as gistemp_src
from app.fetchers import methane as methane_src
from app.fetchers import oni as oni_src
from app.fetchers import sea_ice as sea_ice_src
from app.models import calibration as calibration_model
from app.models import co2 as co2_model
from app.models import markets
from app.models import methane as methane_model
from app.models import sea_ice as sea_ice_model
from app.models import temperature as temperature_model

FIXTURES = Path(os.path.dirname(__file__)) / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ─── Fetcher parsers ───────────────────────────────────────────────────────────

def test_co2_parser_produces_monthly_ppm():
    series = co2_src.parse(_load("co2_sample.csv"))
    assert len(series) == 24
    first = series[0]
    assert first == {"year": 2023, "month": 2, "decimal_date": 2023.125, "ppm": 420.20}
    last = series[-1]
    assert last["year"] == 2025 and last["month"] == 1
    assert 420 < last["ppm"] < 430


def test_co2_parser_skips_comments_and_negatives():
    text = "# header\n# more header\n2024,1,2024.042,-99.99,...\n2024,2,2024.125,425.0,..."
    series = co2_src.parse(text)
    assert len(series) == 1
    assert series[0]["ppm"] == 425.0


def test_methane_parser_produces_monthly_ppb():
    series = methane_src.parse(_load("methane_sample.csv"))
    assert len(series) == 12
    assert series[0]["ppb"] == 1929.41
    # Series should be ordered by month
    assert [s["month"] for s in series] == list(range(1, 13))


def test_gistemp_parser_handles_partial_year():
    parsed = gistemp_src.parse(_load("gistemp_sample.csv"))
    assert parsed is not None
    monthly = parsed["monthly"]
    annual = parsed["annual"]
    # 2025 has 8 months observed (Jan-Aug), Sep-Dec are "***"
    months_2025 = [m for m in monthly if m["year"] == 2025]
    assert len(months_2025) == 8
    assert months_2025[0]["anomaly_c"] == 1.10
    # 2024 should have a complete year + an annual entry
    assert any(a["year"] == 2024 for a in annual)
    # Partial year should NOT appear in annual
    assert not any(a["year"] == 2025 for a in annual)


def test_gistemp_parser_returns_none_when_header_missing():
    assert gistemp_src.parse("nothing useful here") is None


def test_seaice_parser_skips_two_header_rows():
    series = sea_ice_src.parse(_load("seaice_sample.csv"))
    assert len(series) == 8
    assert series[0] == {"year": 2024, "month": 1, "day": 1, "extent_mkm2": 13.453}
    # Min within the sample falls in mid-September
    sept = [s for s in series if s["month"] == 9]
    assert min(s["extent_mkm2"] for s in sept) < 5.0


def test_oni_parser_skips_sentinel_minus99():
    series = oni_src.parse(_load("oni_sample.txt"))
    months_2025 = [s for s in series if s["year"] == 2025]
    # Only Jan-Mar 2025 are real values; the rest are -99 sentinels
    assert len(months_2025) == 3
    assert all(s["month"] in (1, 2, 3) for s in months_2025)


def test_oni_state_classification():
    assert oni_src.state_for(1.5) == "El Niño"
    assert oni_src.state_for(0.5) == "El Niño"
    assert oni_src.state_for(0.0) == "Neutral"
    assert oni_src.state_for(-0.5) == "La Niña"


# ─── Math utilities ────────────────────────────────────────────────────────────

def test_normal_cdf_known_values():
    assert abs(math_utils.normal_cdf(0.0) - 0.5) < 1e-9
    assert abs(math_utils.normal_cdf(1.0) - 0.8413447) < 1e-5
    assert abs(math_utils.normal_cdf(-1.0) - 0.1586552) < 1e-5


def test_linear_regression_recovers_known_line():
    # y = 2x + 5, no noise
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0 * x + 5.0 for x in xs]
    slope, intercept, sigma = math_utils.linear_regression(xs, ys)
    assert abs(slope - 2.0) < 1e-9
    assert abs(intercept - 5.0) < 1e-9
    assert sigma < 1e-9


def test_linear_regression_returns_none_for_degenerate_input():
    assert math_utils.linear_regression([1.0], [2.0]) is None
    assert math_utils.linear_regression([1.0, 1.0], [2.0, 3.0]) is None  # zero variance in x


# ─── Models on fixture data ────────────────────────────────────────────────────

def test_co2_projection_against_fixture():
    raw = co2_src.parse(_load("co2_sample.csv"))
    proj = co2_model.projection({"monthly": raw})
    assert proj is not None
    assert proj["current_year"] == 2025
    # Slope should be plausible — Mauna Loa rises ~2-3 ppm/yr
    assert 1.5 < proj["ppm_per_year"] < 4.0
    assert proj["residual_std_ppm"] >= 0.3  # floor


def test_methane_projection_against_fixture():
    raw = methane_src.parse(_load("methane_sample.csv"))
    proj = methane_model.projection({"monthly": raw})
    assert proj is not None
    assert proj["residual_std_ppb"] >= 2.0  # floor


def test_temperature_projection_against_fixture():
    parsed = gistemp_src.parse(_load("gistemp_sample.csv"))
    proj = temperature_model.projection(parsed)
    assert proj is not None
    assert proj["current_year"] == 2025
    assert proj["months_observed"] == 8
    # 2024 (1.29) is the current record in the fixture
    assert proj["current_record"]["year"] == 2024


def test_threshold_probs_monotone_decreasing():
    parsed = gistemp_src.parse(_load("gistemp_sample.csv"))
    proj = temperature_model.projection(parsed)
    out = temperature_model.threshold_probs(proj)
    probs = [t["p_at_or_above"] for t in out["thresholds"]]
    # P(≥T) should be non-increasing in T
    assert all(probs[i] >= probs[i + 1] for i in range(len(probs) - 1))


# ─── Market scoring ────────────────────────────────────────────────────────────

def test_co2_market_above_ppm_threshold():
    proj = {"projected_year_end_ppm": 426.0, "residual_std_ppm": 0.5}
    p = markets.co2_threshold_market_p("Will CO₂ exceed 425 ppm in 2025?", proj)
    assert p is not None and 0.95 < p < 1.0


def test_co2_market_below_ppm_threshold():
    proj = {"projected_year_end_ppm": 426.0, "residual_std_ppm": 0.5}
    p = markets.co2_threshold_market_p("Will CO₂ stay below 430 ppm in 2025?", proj)
    assert p is not None and 0.99 < p <= 1.0


def test_methane_market_handles_ppm_unit():
    # 1.95 ppm == 1950 ppb, projection is below → low probability of exceeding
    proj = {"projected_year_end_ppb": 1940.0, "residual_std_ppb": 5.0}
    p = markets.methane_threshold_market_p("Will methane exceed 1.95 ppm?", proj)
    assert p is not None and p < 0.05


def test_temperature_anomaly_market_above():
    proj = {"projected_annual_anomaly_c": 1.55, "drift_std_c": 0.05}
    p = markets.temperature_anomaly_market_p("Will the global anomaly exceed 1.5°C?", proj)
    assert p is not None and p > 0.5


def test_temperature_anomaly_market_rejects_implausible_threshold():
    # "0.1°C" is below the [0.5, 3.0] anomaly band, so we should not score it.
    proj = {"projected_annual_anomaly_c": 1.55, "drift_std_c": 0.05}
    assert markets.temperature_anomaly_market_p("Anomaly above 0.1°C?", proj) is None


def test_ice_market_between():
    proj = {"projected_min_mkm2": 4.5, "residual_std_mkm2": 0.3}
    p = markets.ice_min_market_p("Arctic minimum between 4.0m and 5.0m", proj)
    assert p is not None and p > 0.8


# ─── Sea-ice models ────────────────────────────────────────────────────────────

def test_sea_ice_daily_record_check_handles_thin_fixture():
    # The fixture only has one calendar year, so the same-DOY history is empty
    # and daily_record_check returns None — verifies it doesn't crash.
    series = sea_ice_src.parse(_load("seaice_sample.csv"))
    assert sea_ice_model.daily_record_check({"arctic": series}) is None


def test_sea_ice_min_projection_needs_history():
    # Fewer than 10 distinct years of history → returns None
    series = sea_ice_src.parse(_load("seaice_sample.csv"))
    assert sea_ice_model.arctic_min_projection({"arctic": series}) is None


# ─── Calibration summary ───────────────────────────────────────────────────────

def test_calibration_summary_known_values():
    rows = [
        {"error_ppm": 0.5},
        {"error_ppm": -0.3},
        {"error_ppm": 0.1},
        {"error_ppm": -0.1},
    ]
    s = calibration_model.summary(rows, "error_ppm", "ppm")
    assert s["n"] == 4
    assert s["mae"] == round((0.5 + 0.3 + 0.1 + 0.1) / 4, 3)
    assert s["bias"] == round((0.5 - 0.3 + 0.1 - 0.1) / 4, 3)
    assert s["unit"] == "ppm"


def test_calibration_summary_returns_none_when_empty():
    assert calibration_model.summary([], "error_c", "°C") is None
    assert calibration_model.summary([{"foo": 1}], "error_c", "°C") is None
