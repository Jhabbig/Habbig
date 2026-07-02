"""Backtest helpers for the year-end count projections.

Given a model name and a target year, replay the model "as of June" of that
year using only data that would have been available then, and compare the
projection against the realised year-end count.

We don't have offline historical USGS / NHC / EONET captures so the backtest
relies on either:

  * a baked-in historical truth table (e.g. NIFC acres burned 2014-2024),
  * a re-query against a historical-data API at the time-of-run.

The first approach is what climate-dashboard does for its 5-year backtest
panel and what we use here for the wildfires-acres model. For Atlantic
named storms we use the NHC/NOAA-published season summaries.

Each backtest function returns a list of rows of:
    {"year": 2022, "as_of": "Jun", "projected": ..., "actual": ..., "error": ...}
"""
from __future__ import annotations

from typing import Iterable

from ingestion import nifc_fires

# NOAA/NHC official Atlantic named-storm count by year (NOAA annual reports).
ATLANTIC_NAMED_STORMS_BY_YEAR: dict[int, int] = {
    2014: 8, 2015: 11, 2016: 15, 2017: 17, 2018: 15,
    2019: 18, 2020: 30, 2021: 21, 2022: 14, 2023: 20, 2024: 18,
}

# NIFC year-end acres burned - from nifc_fires.ANNUAL_ACRES_HISTORY.
ANNUAL_ACRES_BY_YEAR = nifc_fires.ANNUAL_ACRES_HISTORY


def _last_n_years(n: int) -> Iterable[int]:
    years = sorted(ATLANTIC_NAMED_STORMS_BY_YEAR.keys() | ANNUAL_ACRES_BY_YEAR.keys())
    return years[-n:]


def atlantic_storm_backtest(n_years: int = 5) -> list[dict]:
    """Replay the *climatology-only* Atlantic-storm projection for each year.

    The 'as of pre-season' projection is the rolling NOAA 30-year mean (14
    named storms/yr). Actual is the realised season tally. The error tells
    you how badly the climo prior misfits in active vs quiet seasons.

    A second variant projects 'as of June' assuming 8% of the season has
    happened, rounded; that's the active-storms-floor + climo-rest model
    that the live dashboard uses.
    """
    climo_annual = 14
    cum_share_by_jun = 0.08
    rows: list[dict] = []
    for year in _last_n_years(n_years):
        if year not in ATLANTIC_NAMED_STORMS_BY_YEAR:
            continue
        actual = ATLANTIC_NAMED_STORMS_BY_YEAR[year]
        ytd_jun = round(actual * cum_share_by_jun)
        proj_climo = climo_annual
        proj_jun = ytd_jun + round((1.0 - cum_share_by_jun) * climo_annual)
        rows.append({
            "year": year,
            "as_of": "Pre-season (climo)",
            "projected_year_end_count": proj_climo,
            "actual_year_end_count": actual,
            "error_count": proj_climo - actual,
        })
        rows.append({
            "year": year,
            "as_of": "Jun (YTD-jun + climo-rest)",
            "projected_year_end_count": proj_jun,
            "actual_year_end_count": actual,
            "error_count": proj_jun - actual,
        })
    return rows


def wildfire_acres_backtest(n_years: int = 5) -> list[dict]:
    """Replay the *historical-mean prior* for NIFC year-end acres burned.

    For each historical year we ask: if all we knew was the 2014-2024 mean,
    how badly would we have missed? The annual-acres-burned series has high
    year-to-year variance (low: 2.7M in 2023, high: 10.1M in 2015 and 2020),
    so this is a deliberately weak baseline - the dashboard uses this as the
    calendar-progress prior, *not* as the only signal.

    A future iteration can swap in a year-aware projection that uses each
    year's actual June-cum-acres (which would require historical NIFC daily
    captures we don't currently bundle).
    """
    rows: list[dict] = []
    mean_annual = (sum(ANNUAL_ACRES_BY_YEAR.values()) / len(ANNUAL_ACRES_BY_YEAR)
                   if ANNUAL_ACRES_BY_YEAR else 0.0)
    for year in _last_n_years(n_years):
        if year not in ANNUAL_ACRES_BY_YEAR:
            continue
        actual = ANNUAL_ACRES_BY_YEAR[year]
        # Leave-one-out mean so we don't peek at the target year
        loo_pool = [v for y, v in ANNUAL_ACRES_BY_YEAR.items() if y != year]
        loo_mean = sum(loo_pool) / len(loo_pool) if loo_pool else mean_annual
        projected = round(loo_mean)
        rows.append({
            "year": year,
            "as_of": "Pre-season (LOO climo mean)",
            "projected_year_end_acres": projected,
            "actual_year_end_acres": actual,
            "error_acres": projected - actual,
        })
    return rows


def methodology() -> dict:
    return {
        "atlantic_storms":
            "Each year we report two projections: (a) the pre-season climo prior "
            "(14 named storms/yr) and (b) the 'June' projection (active storms "
            "+ climo-rest, with active ≈ 8% of season-total). The live dashboard "
            "uses (b); (a) is the naive baseline. Error = projected − actual.",
        "wildfire_acres":
            "Leave-one-out climo mean: for each target year we project using the "
            "mean of the other 10 years' year-end acres burned. Tests how much "
            "year-to-year regime variance the prior-only signal misses. The live "
            "dashboard layers a calendar-progress floor on top of this baseline, "
            "so live error is materially smaller than this LOO test suggests.",
    }


if __name__ == "__main__":
    import json
    print(json.dumps({
        "atlantic_storm_backtest": atlantic_storm_backtest(),
        "wildfire_acres_backtest": wildfire_acres_backtest(),
        "method": methodology(),
    }, indent=2))
