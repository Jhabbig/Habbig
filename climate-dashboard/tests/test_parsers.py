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
from app.fetchers import n2o as n2o_src
from app.fetchers import oni as oni_src
from app.fetchers import owid_emissions as emissions_src
from app.fetchers import sea_ice as sea_ice_src
from app.fetchers import sf6 as sf6_src
from app.models import calibration as calibration_model
from app.models import co2 as co2_model
from app.models import emissions as emissions_model
from app.models import forcing as forcing_model
from app.models import highlights as highlights_model
from app.models import markets
from app.models import methane as methane_model
from app.models import n2o as n2o_model
from app.models import sea_ice as sea_ice_model
from app.models import sf6 as sf6_model
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


def test_n2o_parser_produces_monthly_ppb():
    series = n2o_src.parse(_load("n2o_sample.csv"))
    assert len(series) == 24
    assert series[0]["year"] == 2023 and series[0]["month"] == 1
    assert series[-1]["ppb"] > series[0]["ppb"]  # monotone-ish rise


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


def test_n2o_projection_against_fixture():
    raw = n2o_src.parse(_load("n2o_sample.csv"))
    proj = n2o_model.projection({"monthly": raw})
    assert proj is not None
    assert proj["residual_std_ppb"] >= 0.3  # tightest floor of the three
    # N₂O rises ~1 ppb/yr in reality; fixture should give a positive trend
    assert 0.5 < proj["ppb_per_year"] < 3.0


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


def test_sea_ice_annual_extremes():
    series = sea_ice_src.parse(_load("seaice_sample.csv"))
    out = sea_ice_model.annual_extremes(series)
    assert len(out) == 1 and out[0]["year"] == 2024
    # Min in our fixture is the mid-September row (4.276)
    assert out[0]["min_mkm2"] == 4.276
    assert out[0]["max_mkm2"] == 13.605
    # Empty input → empty list, not a crash
    assert sea_ice_model.annual_extremes([]) == []


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


# ─── Regression tests for bugs found in the Phase-4 code review ────────────────

def test_arctic_is_post_min_handles_october_onwards():
    # B1: the original buggy formula was `month >= 9 and day >= 15`, which
    # incorrectly returned False for Oct 1, Nov 14, etc — silently dropping
    # the current year from the Arctic fit for ~3 months of the year.
    from app.models.sea_ice import _is_post_arctic_min
    assert _is_post_arctic_min(9, 14) is False
    assert _is_post_arctic_min(9, 15) is True
    assert _is_post_arctic_min(10, 1) is True
    assert _is_post_arctic_min(11, 14) is True
    assert _is_post_arctic_min(12, 31) is True


def test_safe_implied_rejects_zero_and_missing():
    # B2: market dicts without lastTradePrice and bestBid used to fall back to
    # implied=0.0, producing monstrous +95pp edges against non-existent prices.
    from app.models.markets import _safe_implied
    assert _safe_implied({}) is None
    assert _safe_implied({"lastTradePrice": None, "bestBid": None}) is None
    assert _safe_implied({"lastTradePrice": 0}) is None
    assert _safe_implied({"lastTradePrice": 1}) is None
    assert _safe_implied({"lastTradePrice": 0.42}) == 0.42
    assert _safe_implied({"bestBid": "0.31"}) == 0.31
    # lastTradePrice takes precedence if usable
    assert _safe_implied({"lastTradePrice": 0.55, "bestBid": 0.6}) == 0.55
    # If lastTradePrice is junk, fall through to bestBid
    assert _safe_implied({"lastTradePrice": "junk", "bestBid": 0.4}) == 0.4


def test_temperature_record_excludes_current_year():
    # B3: once GISTEMP publishes the J-D row for the current year, that row
    # would be picked as "current record" and p_breaks_record collapsed to
    # ~0.5 even when the projection IS the actual record.
    gist = {
        "monthly": [{"year": 2024, "month": m, "anomaly_c": 1.2} for m in range(1, 13)]
                 + [{"year": 2025, "month": m, "anomaly_c": 1.4} for m in range(1, 9)],
        "annual": [
            {"year": 2022, "anomaly_c": 0.9},
            {"year": 2023, "anomaly_c": 1.0},
            {"year": 2024, "anomaly_c": 1.2},
            {"year": 2025, "anomaly_c": 1.4},  # current year's annual mean published
        ],
    }
    proj = temperature_model.projection(gist)
    # Record must NOT be 2025 — that's the current year. The prior-year max is 2024.
    assert proj["current_record"]["year"] == 2024


def test_co2_market_accepts_four_digit_threshold():
    # B4: regex used to be \d{3}(?:\.\d+)? — refused to match "1000 ppm".
    # Picking a threshold within ~1σ of the projection so we get a meaningful
    # probability instead of one that underflows to exactly 0 or 1.
    proj = {"projected_year_end_ppm": 1005.0, "residual_std_ppm": 5.0}
    p = markets.co2_threshold_market_p("Will CO₂ exceed 1000 ppm in 2050?", proj)
    assert p is not None and 0.5 < p < 1


def test_methane_ppm_branch_works_without_literal_methane_word():
    # B5: ppm branch used to require literal "methane" in the question text.
    # In practice the outer routing already filters by "methane"/"ch4"/"ppb",
    # so the inner guard was redundant AND broke "Will CH₄ exceed 1.95 ppm?".
    proj = {"projected_year_end_ppb": 1940.0, "residual_std_ppb": 5.0}
    p = markets.methane_threshold_market_p("Will CH4 exceed 1.95 ppm?", proj)
    assert p is not None  # would have returned None pre-fix
    assert p < 0.05  # 1.95 ppm == 1950 ppb, projection is 1940


def test_ice_extent_market_handles_km_unit():
    # B6: regex used to require literal "m" right after the number, so
    # "below 4.5 km²" without "million" silently failed.
    proj = {"projected_min_mkm2": 4.5, "residual_std_mkm2": 0.3}
    # "million km²" — m of "million" consumed the unit char
    p1 = markets.ice_min_market_p("Arctic minimum below 4 million km²", proj)
    assert p1 is not None and 0 < p1 < 1
    # Plain "km²" — needs the [mk] alternation we added
    p2 = markets.ice_min_market_p("Arctic minimum below 4.5 km²", proj)
    assert p2 is not None and 0 < p2 < 1


def test_highlights_detects_temperature_record():
    gist = {
        "annual": [
            {"year": 2020, "anomaly_c": 1.02},
            {"year": 2021, "anomaly_c": 0.86},
            {"year": 2022, "anomaly_c": 0.90},
            {"year": 2023, "anomaly_c": 1.17},
            {"year": 2024, "anomaly_c": 1.29},  # new record
        ],
    }
    items = highlights_model.compute(gistemp=gist)
    texts = [i["text"] for i in items]
    assert any("2024" in t and "record" in t.lower() for t in texts)
    # 2023 (1.17) and 2024 (1.29) are both above +1.0°C — a 2-year streak.
    # 2022 (0.90) breaks it. We expect exactly that to be highlighted.
    streak_lines = [t for t in texts if "consecutive year" in t and "+1.0°C" in t]
    assert streak_lines, texts
    assert "2 consecutive years" in streak_lines[0]


def test_highlights_co2_12month_change():
    co2 = {
        "monthly": [
            {"year": 2024, "month": m, "ppm": 420.0 + 0.2 * m}
            for m in range(1, 13)
        ] + [
            {"year": 2025, "month": m, "ppm": 423.0 + 0.2 * m}
            for m in range(1, 7)
        ],
    }
    items = highlights_model.compute(co2=co2)
    texts = [i["text"] for i in items]
    co2_lines = [t for t in texts if "CO₂" in t and "12 months" in t]
    assert co2_lines, texts
    # Latest is 423 + 0.2*6 = 424.2; 13 months prior is 420 + 0.2*6 = 421.2
    # Delta should be ~+3.0
    assert "+3.00 ppm" in co2_lines[0] or "+3.0 ppm" in co2_lines[0]


def test_highlights_enso_streak():
    # 5 consecutive El Niño months
    oni = {"monthly": [
        {"year": 2024, "month": 8, "oni": 0.3},   # neutral
        {"year": 2024, "month": 9, "oni": 0.6},   # el niño
        {"year": 2024, "month": 10, "oni": 0.8},  # el niño
        {"year": 2024, "month": 11, "oni": 1.0},  # el niño
        {"year": 2024, "month": 12, "oni": 1.2},  # el niño
        {"year": 2025, "month": 1, "oni": 1.1},   # el niño
    ]}
    items = highlights_model.compute(oni=oni)
    texts = [i["text"] for i in items]
    enso_lines = [t for t in texts if "ENSO" in t]
    assert enso_lines, texts
    assert "El Niño" in enso_lines[0] and "5 consecutive months" in enso_lines[0]


def test_highlights_returns_nothing_for_quiet_data():
    # Boring: no records, no streaks, neutral ENSO
    gist = {"annual": [{"year": 2024, "anomaly_c": 0.5}]}
    oni = {"monthly": [{"year": 2024, "month": 12, "oni": 0.1}]}
    items = highlights_model.compute(gistemp=gist, oni=oni)
    # The single year doesn't count as a "new record" (no prior to compare to),
    # and 0.5°C doesn't trigger the >1.0°C streak.
    assert items == []


def test_sf6_parser_and_projection():
    raw = sf6_src.parse(_load("sf6_sample.csv"))
    assert len(raw) == 24
    assert raw[0]["ppt"] == 11.10
    proj = sf6_model.projection({"monthly": raw})
    assert proj is not None
    # SF6 rises ~0.3 ppt/yr (we encoded that linearly in the fixture)
    assert 0.2 < proj["ppt_per_year"] < 0.5
    assert proj["residual_std_ppt"] >= 0.05  # floor


def test_forcing_returns_none_without_co2():
    # CO₂ is required — without it nothing else is meaningful
    assert forcing_model.compute(co2=None, methane={"latest": {"ppb": 1900}}) is None


def test_forcing_co2_only_matches_alpha_ln_ratio():
    # With only CO₂ at the pre-industrial value, forcing should be ~0.
    pre = {"latest": {"ppm": 278.0}}
    result = forcing_model.compute(co2=pre)
    assert abs(result["co2_wm2"]) < 1e-9
    assert abs(result["total_wm2"]) < 1e-9
    # Effective CO₂ ppm equals the input when only CO₂ contributes
    assert abs(result["effective_co2_ppm"] - 278.0) < 0.01


def test_forcing_current_conditions_sane():
    # Approximately today's values: 425 ppm CO₂, 1925 ppb CH₄, 337 ppb N₂O,
    # 11.5 ppt SF₆. Total anthropogenic forcing should be in the IPCC AR6
    # ballpark of ~3.0-3.3 W/m².
    payload = forcing_model.compute(
        co2={"latest": {"ppm": 425.0}},
        methane={"latest": {"ppb": 1925.0}},
        n2o={"latest": {"ppb": 337.0}},
        sf6={"latest": {"ppt": 11.5}},
    )
    assert payload["co2_wm2"] > 1.8 and payload["co2_wm2"] < 2.5
    # CH4 and N2O each contribute several tenths of a W/m²
    assert 0.3 < payload["ch4_wm2"] < 0.7
    assert 0.15 < payload["n2o_wm2"] < 0.35
    # SF6 is small but non-zero
    assert payload["sf6_wm2"] > 0
    # Total in the right ballpark
    assert 2.5 < payload["total_wm2"] < 3.8
    # Effective CO2 framing — should be in the upper-400s ppm range
    assert 450 < payload["effective_co2_ppm"] < 600
    assert payload["have_all_gases"] is True


def test_owid_emissions_parser_buckets_by_country():
    parsed = emissions_src.parse(_load("owid_emissions_sample.csv"))
    assert parsed["latest_year"] == 2022
    # ISO-3 keys for real countries
    assert "CHN" in parsed["countries"]
    assert "USA" in parsed["countries"]
    # World aggregate has the OWID_ prefix
    assert "OWID_WRL" in parsed["countries"]
    # Per-country, year-keyed data with CO2 and per-capita
    chn_2022 = parsed["countries"]["CHN"]["data"][2022]
    assert chn_2022["co2_mt"] == 11396.0
    assert chn_2022["co2_per_capita_t"] == 8.0
    assert chn_2022["share_global"] == 30.7


def test_top_emitters_excludes_regional_aggregates():
    parsed = emissions_src.parse(_load("owid_emissions_sample.csv"))
    top = emissions_model.top_emitters(parsed, n=5)
    isos = [r["iso"] for r in top]
    # Should NOT include OWID_WRL, OWID_ASI, OWID_EUR
    assert all(not iso.startswith("OWID_") for iso in isos)
    # China should be #1 (largest absolute emissions)
    assert top[0]["iso"] == "CHN"
    assert top[0]["co2_mt"] > top[1]["co2_mt"]
    # All five are ISO-3 codes
    assert all(len(iso) == 3 for iso in isos)


def test_top_emitters_handles_missing_data():
    assert emissions_model.top_emitters(None) == []
    assert emissions_model.top_emitters({"countries": {}}) == []


def test_global_summary_computes_decade_change():
    parsed = emissions_src.parse(_load("owid_emissions_sample.csv"))
    g = emissions_model.global_summary(parsed)
    assert g is not None
    assert g["year"] == 2022
    assert g["global_co2_mt"] == 37154.0
    assert g["decade_ago_year"] == 2012
    assert g["decade_ago_co2_mt"] == 35043.0
    # +6.0% over the decade
    assert 5 < g["decade_change_pct"] < 7


def test_n2o_market_routed_correctly():
    # B18: edges_for_markets didn't pass n2o_proj, so any N₂O market would
    # never get a model probability. Verifies routing now picks it up.
    n2o_proj = {"projected_year_end_ppb": 340.0, "residual_std_ppb": 0.5,
                "ppb_per_year": 1.0}
    fake_market = {
        "_event_title": "Atmospheric N₂O in 2026",
        "question": "Will atmospheric N2O exceed 342 ppb in 2026?",
        "lastTradePrice": 0.30,
    }
    enriched = markets.edges_for_markets([fake_market], None, None, None, None, None, n2o_proj)
    assert enriched[0]["_model_p"] is not None
    assert enriched[0]["_edge_pp"] is not None
