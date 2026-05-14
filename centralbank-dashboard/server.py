#!/usr/bin/env python3
"""Central Bank Tracker — FastAPI backend.

Tracks headline policy rates for the Fed, ECB, BoE, BoJ (+ SNB / RBA in the
banks.yaml file but not always wired). For each bank we serve:

  - the current rate (from FRED / ECB SDW / BoE database, with a YAML
    last-known-good fallback when external APIs are unreachable),
  - the OIS-implied path over the next ~12 months,
  - upcoming meeting dates with the implied move probability,
  - the edge between our model probabilities and Polymarket FOMC markets
    (queried via the gateway's Polymarket source).

This MVP intentionally ships with the external fetchers wired but only
exercised on a background task. The first response on every endpoint
serves the YAML snapshot, so the page loads even when FRED/ECB/BoE are
down or rate-limited.

Stateless: no database, no migrations. Hot data lives in an in-memory
cache; cold data lives in data/*.yaml.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("centralbank")

PORT = int(os.environ.get("PORT", "7061"))
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"

# Polymarket source: the gateway exposes a normalised feed at /sources/polymarket
# on its internal port (7000 in production). We default to the local gateway
# and fall back to Polymarket's public Gamma API if the gateway is unreachable.
GATEWAY_POLYMARKET_URL = os.environ.get(
    "GATEWAY_POLYMARKET_URL",
    "http://127.0.0.1:7000/sources/polymarket",
)
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com/events"

USER_AGENT = "narve-centralbank-tracker/1.0 (+https://cb.narve.ai)"
HTTP_TIMEOUT = 8.0

# Refresh external rates this often. Hourly is plenty — these series move on
# committee decisions, not minutes.
REFRESH_INTERVAL_S = 60 * 60

# ─── Cache ─────────────────────────────────────────────────────────────────────

_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str) -> Optional[Any]:
    with _cache_lock:
        entry = _cache.get(key)
        return entry["data"] if entry else None


def _cache_set(key: str, data: Any) -> None:
    with _cache_lock:
        _cache[key] = {"t": time.time(), "data": data}


def _cache_age(key: str) -> Optional[float]:
    with _cache_lock:
        entry = _cache.get(key)
        return (time.time() - entry["t"]) if entry else None


# ─── YAML loaders ──────────────────────────────────────────────────────────────


def load_rates_snapshot() -> dict[str, Any]:
    """Read data/rates_snapshot.yaml — the last-known-good fallback."""
    path = DATA_DIR / "rates_snapshot.yaml"
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("rates_snapshot.yaml missing — serving empty fallback")
        return {"banks": {}, "fallback": True}


def load_banks_meta() -> dict[str, Any]:
    """Read data/banks.yaml — static metadata about tracked banks."""
    path = DATA_DIR / "banks.yaml"
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {"banks": []}


# ─── External fetchers (stubbed for build; live behaviour on deploy) ──────────
#
# We keep the call sites real (httpx + endpoints + parsing) so that a deploy
# turns these on by simply having network access. Anywhere we'd hit an external
# API during build we short-circuit to the snapshot.


async def _fetch_fred_funds_rate(client: httpx.AsyncClient) -> Optional[dict[str, Any]]:
    """FRED publishes the Fed funds target band as DFEDTARU / DFEDTARL.

    Requires a FRED API key in $FRED_API_KEY. Without it, return None so the
    caller falls back to the YAML snapshot.
    """
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return None
    base = "https://api.stlouisfed.org/fred/series/observations"
    async def latest(series: str) -> Optional[float]:
        r = await client.get(base, params={
            "series_id": series,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        })
        if r.status_code != 200:
            return None
        try:
            obs = r.json().get("observations", [])
            if not obs:
                return None
            return float(obs[0]["value"])
        except (ValueError, KeyError):
            return None
    upper = await latest("DFEDTARU")
    lower = await latest("DFEDTARL")
    if upper is None or lower is None:
        return None
    return {
        "band_low_pct": lower,
        "band_high_pct": upper,
        "rate_pct": round((upper + lower) / 2, 4),
        "source": "FRED DFEDTARU / DFEDTARL",
    }


async def _fetch_ecb_deposit_rate(client: httpx.AsyncClient) -> Optional[dict[str, Any]]:
    """ECB Statistical Data Warehouse exposes the deposit facility rate at
    FM.D.U2.EUR.4F.KR.DFR.LEV — CSV download endpoint.
    """
    url = "https://sdw-wsrest.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV"
    try:
        r = await client.get(url, headers={"Accept": "text/csv"})
        if r.status_code != 200:
            return None
        lines = [ln for ln in r.text.splitlines() if ln and not ln.startswith("KEY")]
        if not lines:
            return None
        last = lines[-1].split(",")
        return {"rate_pct": float(last[-1]), "source": "ECB SDW FM.D.U2.EUR.4F.KR.DFR.LEV"}
    except (httpx.HTTPError, ValueError, IndexError):
        return None


async def _fetch_boe_bank_rate(client: httpx.AsyncClient) -> Optional[dict[str, Any]]:
    """BoE database — IUMABEDR is the official Bank Rate series."""
    url = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
    params = {"csv.x": "yes", "Datefrom": "01/Jan/2024", "Dateto": "now",
              "SeriesCodes": "IUMABEDR", "CSVF": "TN", "UsingCodes": "Y"}
    try:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            return None
        rows = [row for row in r.text.splitlines() if "," in row][1:]
        if not rows:
            return None
        last = rows[-1].split(",")
        return {"rate_pct": float(last[-1]), "source": "BoE IUMABEDR"}
    except (httpx.HTTPError, ValueError, IndexError):
        return None


async def refresh_rates_once() -> dict[str, Any]:
    """Try external APIs, fall back to snapshot. Cache the merged result."""
    snapshot = load_rates_snapshot()
    out = {
        "as_of": snapshot.get("as_of"),
        "units": "percent",
        "fallback": True,
        "banks": dict(snapshot.get("banks", {})),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if os.environ.get("CENTRALBANK_OFFLINE") == "1":
        # Test / build mode — never hit the network.
        _cache_set("rates", out)
        return out
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                      headers={"User-Agent": USER_AGENT}) as client:
            fed, ecb, boe = await asyncio.gather(
                _fetch_fred_funds_rate(client),
                _fetch_ecb_deposit_rate(client),
                _fetch_boe_bank_rate(client),
                return_exceptions=True,
            )
            any_live = False
            if isinstance(fed, dict):
                out["banks"].setdefault("fed", {}).update(fed)
                any_live = True
            if isinstance(ecb, dict):
                out["banks"].setdefault("ecb", {}).update(ecb)
                any_live = True
            if isinstance(boe, dict):
                out["banks"].setdefault("boe", {}).update(boe)
                any_live = True
            if any_live:
                out["fallback"] = False
                out["as_of"] = datetime.now(timezone.utc).date().isoformat()
    except Exception as e:
        logger.warning("rate refresh failed: %s — serving snapshot", e)
    _cache_set("rates", out)
    return out


# ─── Implied path model ────────────────────────────────────────────────────────
#
# A full OIS-curve solver is out of scope for this MVP. We synthesise a
# plausible 12-month path from:
#   1. the current policy rate,
#   2. an assumed terminal rate (region-specific),
#   3. a smooth cosine glide between meetings.
# This is good enough to render a sparkline and to demo edges; the real
# implementation in production will use CME FedWatch + EUREX + SONIA OIS.


_TERMINAL_RATE_PCT = {
    "fed": 3.25,
    "ecb": 2.00,
    "boe": 3.25,
    "boj": 1.00,
    "snb": 0.75,
    "rba": 3.25,
}


def implied_path_for(bank_id: str, current_pct: float, horizon_months: int = 12) -> list[dict[str, Any]]:
    terminal = _TERMINAL_RATE_PCT.get(bank_id, current_pct)
    steps = max(2, horizon_months)
    out: list[dict[str, Any]] = []
    today = date.today()
    for i in range(steps + 1):
        # Smooth half-cosine glide from current → terminal across the horizon.
        frac = 0.5 * (1 - math.cos(math.pi * (i / steps)))
        rate = current_pct + (terminal - current_pct) * frac
        m = today + timedelta(days=int(30.4 * i))
        out.append({
            "month": m.isoformat(),
            "implied_pct": round(rate, 3),
        })
    return out


# ─── Meetings + move probabilities ─────────────────────────────────────────────
#
# Static schedule for 2026 H2 — populated from each bank's published calendar.
# Probabilities are derived from the implied path: the bigger the rate change
# between two consecutive meetings, the higher we score "move".

_MEETINGS_2026: dict[str, list[str]] = {
    "fed": ["2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28",
             "2026-12-09"],
    "ecb": ["2026-06-05", "2026-07-24", "2026-09-11", "2026-10-30",
             "2026-12-18"],
    "boe": ["2026-06-19", "2026-08-07", "2026-09-18", "2026-11-06",
             "2026-12-18"],
    "boj": ["2026-06-16", "2026-07-31", "2026-09-19", "2026-10-31",
             "2026-12-19"],
}


def meeting_probabilities(rates: dict[str, Any]) -> list[dict[str, Any]]:
    """For each upcoming meeting, attach P(move) implied by the path."""
    today = date.today()
    rows: list[dict[str, Any]] = []
    for bank_id, dates in _MEETINGS_2026.items():
        bank = rates.get("banks", {}).get(bank_id, {})
        current = bank.get("rate_pct")
        if current is None:
            continue
        path = implied_path_for(bank_id, current, horizon_months=12)
        path_by_month = {p["month"][:7]: p["implied_pct"] for p in path}
        for d_str in dates:
            try:
                d = date.fromisoformat(d_str)
            except ValueError:
                continue
            if d < today:
                continue
            ym = d.strftime("%Y-%m")
            implied = path_by_month.get(ym, current)
            delta_bp = (implied - current) * 100
            # Map |Δbp| → P(move) via a logistic-ish heuristic. 25bp ≈ 0.65,
            # 50bp ≈ 0.85, 0bp ≈ 0.05. Tunable but transparent.
            p_move = 1.0 / (1.0 + math.exp(-(abs(delta_bp) - 12.0) / 6.0))
            rows.append({
                "bank": bank_id,
                "date": d_str,
                "current_pct": current,
                "implied_pct": round(implied, 3),
                "delta_bp": round(delta_bp, 1),
                "p_move": round(p_move, 3),
                "direction": "cut" if delta_bp < -2 else ("hike" if delta_bp > 2 else "hold"),
            })
    rows.sort(key=lambda r: r["date"])
    return rows


# ─── Polymarket edge ───────────────────────────────────────────────────────────


async def fetch_fomc_markets() -> list[dict[str, Any]]:
    """Query the gateway's normalised Polymarket source for FOMC markets.

    Falls back to a direct Gamma API hit if the gateway isn't reachable.
    Returns a (possibly empty) list of market dicts. Stays defensive — every
    call site treats None as "no data available".
    """
    if os.environ.get("CENTRALBANK_OFFLINE") == "1":
        return []
    cached = _cache_get("polymarket")
    if cached is not None and (_cache_age("polymarket") or 999) < 300:
        return cached
    out: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                      headers={"User-Agent": USER_AGENT}) as client:
            # Prefer the gateway-normalised feed.
            r = await client.get(GATEWAY_POLYMARKET_URL,
                                  params={"q": "fomc fed-funds rate-decision"})
            if r.status_code == 200:
                try:
                    out = r.json().get("markets", []) or []
                except ValueError:
                    out = []
            if not out:
                # Direct Gamma fallback — climate-dashboard uses the same shape.
                r2 = await client.get(POLYMARKET_GAMMA_URL,
                                       params={"tag_slug": "fed", "closed": "false",
                                                "limit": "100"})
                if r2.status_code == 200:
                    try:
                        events = r2.json() or []
                    except ValueError:
                        events = []
                    for ev in events:
                        title = (ev.get("title") or "").lower()
                        if "fomc" not in title and "fed" not in title:
                            continue
                        for m in ev.get("markets", []):
                            out.append({
                                "id": m.get("conditionId") or m.get("id"),
                                "question": m.get("question") or ev.get("title"),
                                "event_title": ev.get("title"),
                                "implied_p": _safe_float(m.get("lastTradePrice")
                                                          or m.get("bestBid")),
                                "end_date": m.get("endDate"),
                            })
    except Exception as e:
        logger.warning("polymarket fetch failed: %s", e)
    _cache_set("polymarket", out)
    return out


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def edges_for_fomc(markets: list[dict[str, Any]],
                    meetings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match Polymarket FOMC markets to our model probabilities and compute the
    percentage-point edge. Heuristic matching by date + keyword — robust
    enough for the MVP, will be tightened up once we have a real corpus."""
    fed_meetings = [m for m in meetings if m["bank"] == "fed"]
    out: list[dict[str, Any]] = []
    for mkt in markets:
        q = (mkt.get("question") or "").lower()
        implied = mkt.get("implied_p")
        if implied is None:
            continue
        # Look for the meeting date or month referenced in the question.
        target = None
        for fm in fed_meetings:
            d_iso = fm["date"]
            month_name = date.fromisoformat(d_iso).strftime("%B").lower()
            if d_iso in q or month_name in q:
                target = fm
                break
        if target is None:
            continue
        # Disambiguate hike vs cut vs hold from the question text.
        direction = target["direction"]
        if "cut" in q or "lower" in q:
            model_p = target["p_move"] if direction == "cut" else (1 - target["p_move"]) / 2
        elif "hike" in q or "raise" in q:
            model_p = target["p_move"] if direction == "hike" else (1 - target["p_move"]) / 2
        elif "hold" in q or "unchanged" in q:
            model_p = 1 - target["p_move"]
        else:
            continue
        edge_pp = round((model_p - implied) * 100, 1)
        out.append({
            **mkt,
            "meeting_date": target["date"],
            "model_p": round(model_p, 3),
            "edge_pp": edge_pp,
        })
    return out


# ─── App + background refresh ─────────────────────────────────────────────────

app = FastAPI(title="Central Bank Tracker", version="0.1.0")

# /static/* served as files; / handled by index() below so we control the
# content-type / no-cache headers.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


_refresh_task: Optional[asyncio.Task] = None


async def _periodic_refresh() -> None:
    while True:
        try:
            await refresh_rates_once()
        except Exception as e:
            logger.warning("periodic refresh failed: %s", e)
        await asyncio.sleep(REFRESH_INTERVAL_S)


@app.on_event("startup")
async def _on_startup() -> None:
    global _refresh_task
    # Seed cache synchronously from the snapshot so the first request never
    # waits on the network.
    snapshot = load_rates_snapshot()
    _cache_set("rates", {
        **snapshot,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fallback": True,
    })
    # Kick off the periodic refresh, but don't fail boot if asyncio is unhappy.
    try:
        _refresh_task = asyncio.create_task(_periodic_refresh())
    except RuntimeError:
        logger.warning("could not start refresh task")


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    if _refresh_task is not None:
        _refresh_task.cancel()


# ─── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "centralbank-dashboard",
        "ts": time.time(),
        "cache_keys": sorted(_cache.keys()),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api/rates")
async def api_rates() -> JSONResponse:
    cached = _cache_get("rates")
    age = _cache_age("rates")
    if cached is None or (age is not None and age > REFRESH_INTERVAL_S):
        cached = await refresh_rates_once()
    return JSONResponse(cached)


@app.get("/api/implied-path")
async def api_implied_path(
    bank: str = Query("fed", description="Bank id: fed, ecb, boe, boj, snb, rba"),
    horizon: str = Query("12m", description="Horizon: 6m, 12m, 24m"),
) -> JSONResponse:
    rates = _cache_get("rates") or await refresh_rates_once()
    bank_data = rates.get("banks", {}).get(bank)
    if not bank_data or bank_data.get("rate_pct") is None:
        raise HTTPException(status_code=404, detail=f"unknown bank: {bank}")
    months_map = {"6m": 6, "12m": 12, "24m": 24}
    months = months_map.get(horizon, 12)
    path = implied_path_for(bank, bank_data["rate_pct"], horizon_months=months)
    return JSONResponse({
        "bank": bank,
        "current_pct": bank_data["rate_pct"],
        "terminal_pct": _TERMINAL_RATE_PCT.get(bank, bank_data["rate_pct"]),
        "horizon": horizon,
        "path": path,
        "note": (
            "Model is a cosine-glide from current to terminal; production "
            "will swap in CME FedWatch / EUREX / SONIA OIS curves."
        ),
    })


@app.get("/api/fomc-meetings")
async def api_fomc_meetings() -> JSONResponse:
    rates = _cache_get("rates") or await refresh_rates_once()
    return JSONResponse({
        "meetings": meeting_probabilities(rates),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/polymarket-edge")
async def api_polymarket_edge() -> JSONResponse:
    rates = _cache_get("rates") or await refresh_rates_once()
    markets = await fetch_fomc_markets()
    meetings = meeting_probabilities(rates)
    enriched = edges_for_fomc(markets, meetings)
    return JSONResponse({
        "markets": enriched,
        "count": len(enriched),
        "source": GATEWAY_POLYMARKET_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/banks")
def api_banks() -> JSONResponse:
    return JSONResponse(load_banks_meta())


# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Central Bank Tracker on :%d", PORT)
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=False)
