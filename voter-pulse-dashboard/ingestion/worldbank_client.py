"""World Bank Indicators ingestion for sector-employment data.

The World Bank's open Indicators API is the canonical free source for
cross-country sector composition. We pull four series:

  SL.AGR.EMPL.ZS  - Employment in agriculture (% of total employment, modeled ILO)
  SL.IND.EMPL.ZS  - Employment in industry    (% of total employment, modeled ILO)
  SL.SRV.EMPL.ZS  - Employment in services    (% of total employment, modeled ILO)
  NY.GDP.PCAP.CD  - GDP per capita (current USD)

API endpoint shape:
  https://api.worldbank.org/v2/country/all/indicator/{id}
     ?format=json&date={start}:{end}&per_page=20000

The first element of the response is a meta dict; the second is a list of
{country, indicator, date, value, countryiso3code, ...} rows. Values can
be null for years the country didn't report. We pick each country's most
recent non-null observation in the requested window.

No API key required. Cached 24h - these series update annually.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from threading import Lock

log = logging.getLogger(__name__)

_UA = "voter-pulse-dashboard/0.4"
_BASE = "https://api.worldbank.org/v2/country/all/indicator"
_DEFAULT_WINDOW = (2018, 2024)  # search this year-range for the most recent reading

INDICATORS: dict[str, str] = {
    "agriculture": "SL.AGR.EMPL.ZS",
    "industry":    "SL.IND.EMPL.ZS",
    "services":    "SL.SRV.EMPL.ZS",
    "gdp_per_capita": "NY.GDP.PCAP.CD",
}

# Additional per-country indicators surfaced in the country-detail card.
# Same source / no-key API. CSV-style annual cadence.
DETAIL_INDICATORS: dict[str, str] = {
    "inflation_yoy_pct": "FP.CPI.TOTL.ZG",         # CPI inflation, annual %
    "unemployment_pct":  "SL.UEM.TOTL.ZS",         # Unemployment, total %
    "gdp_growth_pct":    "NY.GDP.MKTP.KD.ZG",      # Real GDP growth, annual %
    "life_expectancy":   "SP.DYN.LE00.IN",         # Life expectancy at birth, years
    "population":        "SP.POP.TOTL",            # Total population
}

# Sector-composition time-series window for the country-detail trajectory.
_HISTORY_WINDOW = (1991, 2024)

# Aggregate "countries" we want to exclude (regions, income groups, the world itself).
# The World Bank API returns these mixed in with real countries; filter on ISO3.
_AGG_ISO3 = {
    "WLD", "ARB", "CEB", "EAP", "EAR", "EAS", "ECA", "ECS", "EMU", "EUU",
    "FCS", "HIC", "HPC", "IBD", "IBT", "IDA", "IDB", "IDX", "LAC", "LCN",
    "LDC", "LIC", "LMC", "LMY", "LTE", "MEA", "MIC", "MNA", "NAC", "OED",
    "OSS", "PRE", "PSS", "PST", "SAS", "SSA", "SSF", "SST", "TEA", "TEC",
    "TLA", "TMN", "TSA", "TSS", "UMC", "AFE", "AFW", "CSS", "INX",
}


def _fetch(url: str, timeout: float = 30.0) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def fetch_indicator(indicator_id: str, start: int, end: int) -> dict[str, dict]:
    """Fetch one World Bank series, return {iso3: {value, year, country_name}}
    with each country's most-recent non-null observation in [start, end]."""
    url = f"{_BASE}/{indicator_id}?format=json&date={start}:{end}&per_page=20000"
    try:
        body = _fetch(url)
    except Exception as exc:
        log.warning("World Bank fetch failed for %s: %s", indicator_id, exc)
        return {}
    if not isinstance(body, list) or len(body) < 2 or not isinstance(body[1], list):
        log.warning("Unexpected World Bank payload for %s", indicator_id)
        return {}

    by_iso3: dict[str, dict] = {}
    for row in body[1]:
        iso3 = row.get("countryiso3code") or ""
        if not iso3 or iso3 in _AGG_ISO3:
            continue
        val = row.get("value")
        if val is None:
            continue
        try:
            year = int(row.get("date") or 0)
        except (TypeError, ValueError):
            continue
        # Keep only the most recent year per country
        prev = by_iso3.get(iso3)
        if prev is None or year > prev["year"]:
            by_iso3[iso3] = {
                "year": year,
                "value": float(val),
                "country_name": (row.get("country") or {}).get("value") or iso3,
            }
    return by_iso3


def assemble() -> list[dict]:
    """Pull every indicator and join into a per-country dict."""
    pulls = {name: fetch_indicator(sid, *_DEFAULT_WINDOW) for name, sid in INDICATORS.items()}
    # Use the union of ISO3s from agri / industry / services (GDP is bonus context)
    iso3s: set[str] = set()
    for k in ("agriculture", "industry", "services"):
        iso3s.update(pulls[k].keys())

    out: list[dict] = []
    for iso3 in sorted(iso3s):
        agri = pulls["agriculture"].get(iso3)
        ind  = pulls["industry"].get(iso3)
        srv  = pulls["services"].get(iso3)
        gdp  = pulls["gdp_per_capita"].get(iso3)
        if not (agri and ind and srv):
            continue
        # Use the most-recent year across the three sector series for the headline year
        year = max(agri["year"], ind["year"], srv["year"])
        # Normalise: sometimes the three add to slightly more or less than 100
        # because of modeled-ILO rounding. Renormalize so the ternary maths is exact.
        total = agri["value"] + ind["value"] + srv["value"]
        if total <= 0:
            continue
        a = agri["value"] / total * 100
        i = ind["value"] / total * 100
        s = srv["value"] / total * 100
        out.append({
            "iso3": iso3,
            "name": agri["country_name"],
            "year": year,
            "agriculture_pct": a,
            "industry_pct": i,
            "services_pct": s,
            "gdp_per_capita_usd": gdp["value"] if gdp else None,
            "gdp_year": gdp["year"] if gdp else None,
        })
    return out


# ── Cache ────────────────────────────────────────────────────────────────────
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_CACHE_TTL = 24 * 3600
_lock = Lock()


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = (now - _CACHE["fetched_at"]) < _CACHE_TTL and _CACHE["data"] is not None
        if fresh and not force and _CACHE["fetched_at"]:
            return {"countries": _CACHE["data"], "fetched_at": _CACHE["fetched_at"]}
    rows = assemble()
    with _lock:
        _CACHE["data"] = rows
        _CACHE["fetched_at"] = now
    return {"countries": rows, "fetched_at": now}


# ── Per-country detail (with annual time-series) ─────────────────────────────
# Cached separately so the world summary stays cheap to compute.

_DETAIL_CACHE: dict[str, dict] = {}
_DETAIL_FETCHED_AT: dict[str, float] = {}
_DETAIL_TTL = 24 * 3600
_detail_lock = Lock()


def _fetch_indicator_for_country(indicator_id: str, iso3: str, start: int, end: int) -> list[tuple[int, float]]:
    """Return (year, value) pairs for one country's series, oldest first."""
    url = f"https://api.worldbank.org/v2/country/{iso3}/indicator/{indicator_id}?format=json&date={start}:{end}&per_page=200"
    try:
        body = _fetch(url)
    except Exception as exc:
        log.warning("World Bank country fetch failed %s %s: %s", iso3, indicator_id, exc)
        return []
    if not isinstance(body, list) or len(body) < 2 or not isinstance(body[1], list):
        return []
    out: list[tuple[int, float]] = []
    for row in body[1]:
        v = row.get("value")
        if v is None:
            continue
        try:
            year = int(row.get("date") or 0)
        except (TypeError, ValueError):
            continue
        out.append((year, float(v)))
    out.sort(key=lambda r: r[0])
    return out


def fetch_country_detail(iso3: str) -> dict:
    """Build a full per-country profile: sector trajectory + the detail indicators."""
    iso3 = iso3.upper()
    history_start, history_end = _HISTORY_WINDOW
    sector_pulls = {
        name: _fetch_indicator_for_country(sid, iso3, history_start, history_end)
        for name, sid in {
            "agriculture": INDICATORS["agriculture"],
            "industry":    INDICATORS["industry"],
            "services":    INDICATORS["services"],
        }.items()
    }
    # Build a year-keyed trajectory where all three sectors are present
    years = sorted(set(y for series in sector_pulls.values() for (y, _) in series))
    by_year: dict[int, dict] = {}
    for y in years:
        a = next((v for (yy, v) in sector_pulls["agriculture"] if yy == y), None)
        i = next((v for (yy, v) in sector_pulls["industry"]    if yy == y), None)
        s = next((v for (yy, v) in sector_pulls["services"]    if yy == y), None)
        if a is None or i is None or s is None:
            continue
        total = a + i + s
        if total <= 0:
            continue
        by_year[y] = {
            "year": y,
            "agriculture_pct": a / total * 100,
            "industry_pct":    i / total * 100,
            "services_pct":    s / total * 100,
        }
    trajectory = [by_year[y] for y in sorted(by_year)]

    # Detail indicators — keep latest, plus a short tail for sparklines
    detail: dict[str, dict] = {}
    for name, sid in DETAIL_INDICATORS.items():
        pts = _fetch_indicator_for_country(sid, iso3, history_start, history_end)
        latest = pts[-1] if pts else None
        prior = pts[-2] if len(pts) >= 2 else None
        delta = None
        if latest and prior:
            delta = latest[1] - prior[1]
        detail[name] = {
            "indicator_id": sid,
            "latest": {"year": latest[0], "value": latest[1]} if latest else None,
            "delta_vs_prior_year": delta,
            "series": [{"year": y, "value": v} for (y, v) in pts[-30:]],
        }

    name = None
    iso2 = None
    try:
        body = _fetch(f"https://api.worldbank.org/v2/country/{iso3}?format=json")
        if isinstance(body, list) and len(body) >= 2 and body[1]:
            meta = body[1][0]
            name = meta.get("name")
            iso2 = meta.get("iso2Code")
    except Exception as exc:
        log.warning("World Bank country metadata fetch failed %s: %s", iso3, exc)

    return {
        "iso3": iso3,
        "iso2": iso2,
        "name": name or iso3,
        "trajectory": trajectory,
        "detail": detail,
    }


def get_country_detail_cached(iso3: str, force: bool = False) -> dict:
    iso3 = iso3.upper()
    now = time.time()
    with _detail_lock:
        cached = _DETAIL_CACHE.get(iso3)
        fetched_at = _DETAIL_FETCHED_AT.get(iso3, 0.0)
        fresh = (now - fetched_at) < _DETAIL_TTL and cached is not None
        if fresh and not force:
            return {**cached, "fetched_at": fetched_at}
    data = fetch_country_detail(iso3)
    with _detail_lock:
        _DETAIL_CACHE[iso3] = data
        _DETAIL_FETCHED_AT[iso3] = now
    return {**data, "fetched_at": now}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = get_cached(force=True)
    print(f"{len(out['countries'])} countries with sector data")
    for c in sorted(out["countries"], key=lambda r: r["agriculture_pct"], reverse=True)[:8]:
        print(f"  {c['iso3']} {c['name']:30s} A {c['agriculture_pct']:5.1f}% "
              f"I {c['industry_pct']:5.1f}% S {c['services_pct']:5.1f}% ({c['year']})")
