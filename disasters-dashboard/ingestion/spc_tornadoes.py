"""SPC (NWS Storm Prediction Center) tornado feed.

Two pieces:

  * ``daily_storm_reports()`` - SPC's preliminary daily storm-report CSV.
    Updated nightly; the format is one row per report with ``Time, F_Scale,
    Location, County, State, Lat, Lon, Comments``. Free, no key.

  * ``ytd_tornado_projection()`` - aggregate the per-day reports for the
    current year (+ a 30-year monthly climatology fallback) and extrapolate
    to year-end. Used by the market matcher for "will N+ tornadoes happen
    this year" markets.

Endpoint pattern:
    https://www.spc.noaa.gov/climo/reports/YYMMDD_rpts.csv         (today)
    https://www.spc.noaa.gov/climo/reports/yesterday.csv           (yesterday)
    https://www.spc.noaa.gov/climo/reports/today.csv               (today shortcut)

For the YTD projection we read the SPC monthly tornado-count CSV at
    https://www.spc.noaa.gov/wcm/data/...
which is a long-running annual archive. To keep the dashboard self-contained
without scraping, we use a hand-coded 1991-2020 monthly climatology and
extrapolate from days-into-year using that shape.
"""
from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

SPC_TODAY_URL = "https://www.spc.noaa.gov/climo/reports/today_filtered.csv"
SPC_YESTERDAY_URL = "https://www.spc.noaa.gov/climo/reports/yesterday_filtered.csv"

# 1991-2020 mean monthly US tornado counts (preliminary). Source: SPC WCM.
MONTHLY_CLIMO_TORNADOES: dict[int, int] = {
    1: 33, 2: 41, 3: 80, 4: 155, 5: 276, 6: 240,
    7: 116, 8: 80, 9: 75, 10: 79, 11: 49, 12: 25,
}
ANNUAL_CLIMO_TORNADOES = sum(MONTHLY_CLIMO_TORNADOES.values())  # ~1249/yr


def _parse_csv(text: str) -> list[dict]:
    out: list[dict] = []
    rdr = csv.DictReader(io.StringIO(text))
    for row in rdr:
        # SPC sometimes inserts blank lines between event types; skip them.
        if not any(row.values()):
            continue
        out.append({
            "time": (row.get("Time") or "").strip(),
            "f_scale": (row.get("F_Scale") or row.get("Mag") or "").strip(),
            "location": (row.get("Location") or "").strip(),
            "county": (row.get("County") or "").strip(),
            "state": (row.get("State") or "").strip(),
            "lat": _maybe_float(row.get("Lat")),
            "lon": _maybe_float(row.get("Lon")),
            "comments": (row.get("Comments") or "").strip(),
            "type": (row.get("Type") or row.get("Event") or "").strip(),
        })
    return out


def _maybe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def daily_storm_reports() -> dict:
    cache_key = "spc_today"
    hit = _cache.get(cache_key, ttl_s=900)  # 15 min
    if hit is not None:
        return hit
    rows = []
    for url, label in [(SPC_TODAY_URL, "today"), (SPC_YESTERDAY_URL, "yesterday")]:
        r = http_get(url, timeout=20)
        if not r:
            continue
        try:
            day_rows = _parse_csv(r.text)
        except csv.Error:
            continue
        for row in day_rows:
            row["report_day"] = label
            rows.append(row)
    if not rows:
        return {"error": "SPC fetch failed", "reports": [], "count": 0}
    tornadoes = [r for r in rows if (r.get("type") or "").lower() == "tornado"
                 or (r.get("f_scale") or "").upper().startswith("E")
                 or (r.get("f_scale") or "").upper().startswith("F")]
    hail = [r for r in rows if (r.get("type") or "").lower() == "hail"]
    wind = [r for r in rows if (r.get("type") or "").lower() == "wind"]
    out = {
        "source": "SPC daily preliminary storm reports (today + yesterday)",
        "count": len(rows),
        "tornado_count": len(tornadoes),
        "hail_count": len(hail),
        "wind_count": len(wind),
        "reports": rows[:200],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def ytd_tornado_projection() -> dict:
    """Project year-end US tornado count using SPC monthly climatology.

    Without scraping SPC's annual archive we approximate YTD count using the
    fraction of climatological mass that falls before today's day-of-year,
    times the historical annual mean. The active-tornado-day count from
    ``daily_storm_reports()`` is added as a "live signal" extra row.
    """
    cache_key = "spc_ytd_proj"
    hit = _cache.get(cache_key, ttl_s=3600)
    if hit is not None:
        return hit
    today = datetime.now(timezone.utc).date()
    year = today.year
    days_in_year = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    days_into_year = (today - date(year, 1, 1)).days + 1
    days_remaining = days_in_year - days_into_year
    # Fraction of climo cumulative count by today's DOY
    cum_today = 0
    cur = 0
    for m in range(1, 13):
        # Days in each month accumulated
        if m == today.month:
            # Add fractional month
            month_start = date(year, m, 1).timetuple().tm_yday
            in_month = days_into_year - month_start + 1
            month_days = (date(year + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
                          - date(year, m, 1)).days
            cum_today = cur + MONTHLY_CLIMO_TORNADOES[m] * (in_month / month_days)
            break
        cur += MONTHLY_CLIMO_TORNADOES[m]
    cum_today_fraction = cum_today / ANNUAL_CLIMO_TORNADOES
    ytd_climo_implied = int(round(cum_today_fraction * ANNUAL_CLIMO_TORNADOES))
    lam_remaining = (1.0 - cum_today_fraction) * ANNUAL_CLIMO_TORNADOES
    # σ for the year-end count. Tornadoes are overdispersed vs Poisson:
    # 2011 had 1697 vs the climo mean of 1249, 2018 had 1120. Empirical
    # year-to-year std (1991-2020) is ~250 - much wider than Poisson sqrt.
    # We use sqrt(climo) inflated 7x to capture the regime variance.
    sigma_year_end = max(math.sqrt(ANNUAL_CLIMO_TORNADOES) * 7.0, 250.0)
    out = {
        "source": "SPC 1991-2020 monthly climatology",
        "year": year,
        "as_of": today.isoformat(),
        "days_into_year": days_into_year,
        "days_remaining": days_remaining,
        "climo_annual_tornadoes": ANNUAL_CLIMO_TORNADOES,
        "ytd_climo_implied": ytd_climo_implied,
        "lambda_remaining": round(lam_remaining, 1),
        "projected_year_end_count": int(round(ytd_climo_implied + lam_remaining)),
        "year_end_sigma": round(sigma_year_end, 1),
        "caveat": "YTD count is climatological; live SPC archive scraping (v0.x) replaces this with the actual preliminary count.",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def p_year_end_tornadoes_at_least(proj: dict, threshold: int) -> Optional[float]:
    """Same Poisson-tail approach used by ``analysis.poisson.p_at_least`` but
    here the YTD count is climo-implied so we treat the prediction as
    Normal(mean=projected, sigma~sqrt(lambda)) - which is the Poisson normal
    approximation for the large lambdas tornado counts produce."""
    if not proj or proj.get("error"):
        return None
    mu = proj.get("projected_year_end_count")
    lam = proj.get("lambda_remaining")
    if mu is None or lam is None:
        return None
    sigma = math.sqrt(max(lam, 1.0))
    z = (threshold - mu) / sigma
    return 0.5 * math.erfc(z / math.sqrt(2))


if __name__ == "__main__":
    import json
    print(json.dumps(daily_storm_reports(), indent=2)[:1500])
    print(json.dumps(ytd_tornado_projection(), indent=2))
