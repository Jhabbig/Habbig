"""External data-source clients for historical backfill and live feeds.

Each client returns ``list[tuple[int ts, str key, float value]]`` so the result
flows straight into ``history.record_series()``. Clients without credentials
silently no-op so the dashboard boots cleanly even with no env vars set.

Sources:
    World Bank — public, no key needed (annual country-year indicators).
    FRED       — free with FRED_API_KEY (US/global econ time series).
    ACLED      — free with ACLED_EMAIL/PASSWORD (live armed-conflict events).
"""
from __future__ import annotations

import calendar
import datetime as _dt
import logging
import os
import time
from typing import Any

import httpx

_log = logging.getLogger("data_sources")

# Shared async HTTP client. Module-level so we get connection pooling across
# the lifetime of the process. Lazy-init avoids fighting import order.
_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
            headers={"User-Agent": "narve-world-state/1.0 (data_sources)"},
        )
    return _client


# ── World Bank ──────────────────────────────────────────────────────────────
# No API key. Docs: https://datahelpdesk.worldbank.org/knowledgebase/articles/889392
# Indicators: annual country-year values. We pull all countries for an indicator.
WORLDBANK_INDICATORS = {
    # WB code               → metric key prefix used in the snapshots DB
    "NY.GDP.MKTP.CD":         "wb.gdp_usd",
    "FP.CPI.TOTL.ZG":         "wb.inflation_pct",
    "SP.POP.TOTL":            "wb.population",
    "MS.MIL.XPND.CD":         "wb.milex_usd",
    "VC.IDP.TOCV":            "wb.idps_total",        # internally displaced people
    "SM.POP.REFG.OR":         "wb.refugees_origin",   # refugees by country of origin
    "SM.POP.REFG":            "wb.refugees_dest",     # refugees by country of destination
    "EG.USE.ELEC.KH.PC":      "wb.electricity_kwh_pc",
    "GC.DOD.TOTL.GD.ZS":      "wb.gov_debt_pct_gdp",
}


async def fetch_worldbank_indicator(indicator: str, since_year: int = 1990) -> list[tuple]:
    """Fetch one World Bank indicator across all countries since ``since_year``.

    Returns a flat list of (ts, key, value) tuples. Each (year, country) becomes
    one point. ts = unix timestamp at midnight Jan 1 of that year (UTC).
    """
    key_prefix = WORLDBANK_INDICATORS.get(indicator, f"wb.{indicator.lower().replace('.', '_')}")
    client = await _get_client()
    url = f"https://api.worldbank.org/v2/country/all/indicator/{indicator}"
    out: list[tuple] = []
    page = 1
    while True:
        try:
            r = await client.get(url, params={
                "format": "json", "per_page": "20000", "date": f"{since_year}:2030", "page": page,
            })
            if r.status_code != 200:
                _log.warning("worldbank %s page %d: HTTP %d", indicator, page, r.status_code)
                break
            data = r.json()
        except Exception as e:
            _log.warning("worldbank %s page %d failed: %s", indicator, page, e)
            break
        if not isinstance(data, list) or len(data) < 2:
            break
        meta, rows = data[0], data[1]
        if not rows:
            break
        for row in rows:
            try:
                year = int(row.get("date") or 0)
                value = row.get("value")
                country = (row.get("country") or {}).get("id") or row.get("countryiso3code")
                if value is None or year <= 0 or not country:
                    continue
                # Mid-year (Jul 1) is a sane representative timestamp for an annual value
                ts = calendar.timegm((year, 7, 1, 0, 0, 0, 0, 0, 0))
                out.append((ts, f"{key_prefix}.{country}", float(value)))
            except (TypeError, ValueError):
                continue
        if page >= (meta.get("pages") or 1):
            break
        page += 1
    return out


async def backfill_worldbank(indicators: list[str] | None = None, since_year: int = 1990) -> dict:
    """Backfill multiple World Bank indicators. Returns {indicator: row_count}."""
    selected = indicators or list(WORLDBANK_INDICATORS.keys())
    summary: dict[str, int] = {}
    # Import here to avoid circular at module load
    import history
    for ind in selected:
        points = await fetch_worldbank_indicator(ind, since_year=since_year)
        n = await history.record_series(points)
        summary[ind] = n
        _log.info("worldbank backfill: %s → %d rows", ind, n)
    return summary


# ── FRED (Federal Reserve Economic Data) ────────────────────────────────────
# Free key: https://fred.stlouisfed.org/docs/api/api_key.html
# Set FRED_API_KEY env var to enable.
FRED_SERIES = {
    # FRED series id → metric key
    "DGS10":    "fred.us_10y_yield",
    "DGS2":     "fred.us_2y_yield",
    "T10Y2Y":   "fred.yield_curve_10y2y",
    "VIXCLS":   "fred.vix",
    "DCOILWTICO":"fred.wti_oil",
    "DCOILBRENTEU":"fred.brent_oil",
    "GOLDAMGBD228NLBM":"fred.gold",
    "DEXUSEU":  "fred.eur_usd",
    "DEXCHUS":  "fred.cny_usd",
    "DEXJPUS":  "fred.jpy_usd",
    "M2SL":     "fred.m2_us",
    "UNRATE":   "fred.unrate_us",
    "CPIAUCSL": "fred.cpi_us",
    "DFF":      "fred.fed_funds",
}


def fred_enabled() -> bool:
    return bool(os.environ.get("FRED_API_KEY", "").strip())


async def fetch_fred_series(series_id: str, since: str = "1990-01-01") -> list[tuple]:
    """Fetch one FRED series. Returns (ts, key, value) list. No-op if no key."""
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        return []
    key = FRED_SERIES.get(series_id, f"fred.{series_id.lower()}")
    client = await _get_client()
    try:
        r = await client.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "observation_start": since,
            },
        )
        if r.status_code != 200:
            _log.warning("fred %s: HTTP %d", series_id, r.status_code)
            return []
        data = r.json()
    except Exception as e:
        _log.warning("fred %s failed: %s", series_id, e)
        return []
    out: list[tuple] = []
    for obs in data.get("observations", []):
        v = obs.get("value")
        if v in (None, "", "."):
            continue
        try:
            ts = calendar.timegm(_dt.date.fromisoformat(obs["date"]).timetuple())
            out.append((ts, key, float(v)))
        except (KeyError, ValueError, TypeError):
            continue
    return out


async def backfill_fred(series_ids: list[str] | None = None, since: str = "2000-01-01") -> dict:
    """Backfill FRED series. Returns {series_id: row_count}."""
    if not fred_enabled():
        _log.info("FRED_API_KEY not set; skipping FRED backfill")
        return {}
    selected = series_ids or list(FRED_SERIES.keys())
    summary: dict[str, int] = {}
    import history
    for sid in selected:
        points = await fetch_fred_series(sid, since=since)
        n = await history.record_series(points)
        summary[sid] = n
        _log.info("fred backfill: %s → %d rows", sid, n)
    return summary


# ── ACLED (Armed Conflict Location & Event Data) ────────────────────────────
# Free with registration: https://acleddata.com/data-export-tool/
# Set ACLED_EMAIL + ACLED_PASSWORD or ACLED_ACCESS_TOKEN. OAuth flow ported from
# https://github.com/worldmonitor/worldmonitor/blob/HEAD/scripts/shared/acled-oauth.mjs
# (Apache-2.0).
_ACLED_TOKEN: dict[str, Any] = {"token": None, "expires_at": 0.0}


def acled_enabled() -> bool:
    return bool(
        (os.environ.get("ACLED_EMAIL") and os.environ.get("ACLED_PASSWORD"))
        or os.environ.get("ACLED_ACCESS_TOKEN")
    )


async def _acled_token() -> str | None:
    """Get a valid ACLED OAuth token, caching for the reported lifetime."""
    static = os.environ.get("ACLED_ACCESS_TOKEN", "").strip()
    if static:
        return static
    email = os.environ.get("ACLED_EMAIL", "").strip()
    password = os.environ.get("ACLED_PASSWORD", "").strip()
    if not (email and password):
        return None
    if _ACLED_TOKEN["token"] and time.time() < _ACLED_TOKEN["expires_at"] - 60:
        return _ACLED_TOKEN["token"]
    client = await _get_client()
    try:
        r = await client.post(
            "https://acleddata.com/oauth/token",
            data={"username": email, "password": password, "grant_type": "password", "client_id": "acled"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            _log.warning("acled oauth: HTTP %d (%s)", r.status_code, r.text[:200])
            return None
        d = r.json()
    except Exception as e:
        _log.warning("acled oauth failed: %s", e)
        return None
    tok = d.get("access_token")
    if not tok:
        return None
    _ACLED_TOKEN["token"] = tok
    _ACLED_TOKEN["expires_at"] = time.time() + (d.get("expires_in") or 3600)
    return tok


async def fetch_acled_recent(days: int = 7, limit: int = 500) -> list[dict]:
    """Fetch recent ACLED events. Returns raw event records (not metrics).
    Live data — caller decides what to do with it."""
    tok = await _acled_token()
    if not tok:
        return []
    client = await _get_client()
    cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    try:
        r = await client.get(
            "https://api.acleddata.com/acled/read",
            params={
                "limit": str(limit),
                "event_date": cutoff,
                "event_date_where": ">=",
                "fields": "event_date|event_type|sub_event_type|country|admin1|location|latitude|longitude|fatalities|notes",
            },
            headers={"Authorization": f"Bearer {tok}"},
        )
        if r.status_code != 200:
            _log.warning("acled read: HTTP %d", r.status_code)
            return []
        d = r.json()
    except Exception as e:
        _log.warning("acled read failed: %s", e)
        return []
    return d.get("data", []) if isinstance(d, dict) else []
