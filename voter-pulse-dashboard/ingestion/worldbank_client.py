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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = get_cached(force=True)
    print(f"{len(out['countries'])} countries with sector data")
    for c in sorted(out["countries"], key=lambda r: r["agriculture_pct"], reverse=True)[:8]:
        print(f"  {c['iso3']} {c['name']:30s} A {c['agriculture_pct']:5.1f}% "
              f"I {c['industry_pct']:5.1f}% S {c['services_pct']:5.1f}% ({c['year']})")
