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

The external fetchers are wired through a once-per-hour background task. The
first response on every endpoint serves the YAML snapshot, so the page loads
even when FRED/ECB/BoE are down or rate-limited. Each bank in /api/rates
carries a ``_source`` field — ``live``, ``snapshot_yaml``, or ``stale`` —
so the frontend can show data provenance.

Stateless: no database, no migrations. Hot data lives in an in-memory
cache; cold data lives in data/*.yaml.
"""

from __future__ import annotations

import asyncio
import hmac
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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("centralbank")


# ── BetterStack / Logtail ─────────────────────────────────────────────────────
# Ships structured logs to the central BetterStack source for the "centralbank"
# subproduct. Falls back to the apex LOGTAIL_TOKEN if the per-service variable
# is unset. If neither is set we silently skip — stdout/stderr handlers stay
# attached so logs are never lost.
class _ServiceTagFilter(logging.Filter):
    """Stamps every record with service=<name> so BetterStack can route/group."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        if not hasattr(record, "service"):
            record.service = self._service
        return True


_logtail_token = os.getenv("LOGTAIL_TOKEN_CENTRALBANK", os.getenv("LOGTAIL_TOKEN", "")).strip()
# Always tag local records with the service name so downstream aggregators
# (docker logs -> vector -> wherever) can group correctly even without Logtail.
logging.getLogger().addFilter(_ServiceTagFilter("centralbank"))
if _logtail_token:
    try:
        from logtail import LogtailHandler  # type: ignore

        _handler = LogtailHandler(source_token=_logtail_token)
        _handler.setLevel(logging.INFO)
        _handler.addFilter(_ServiceTagFilter("centralbank"))
        logging.getLogger().addHandler(_handler)
        logger.info("Logtail handler attached", extra={"service": "centralbank"})
    except ImportError:
        logger.warning("logtail-python not installed; skipping BetterStack handler",
                       extra={"service": "centralbank"})
    except Exception as _exc:  # pragma: no cover — defensive: never crash on log init
        logger.warning("Logtail init failed: %s", _exc, extra={"service": "centralbank"})


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

USER_AGENT = "narve.ai centralbank-dashboard"
HTTP_TIMEOUT = 8.0
# Per-fetcher timeout. The spec wants each external call to abort fast so a
# single slow upstream doesn't stall the whole refresh.
FETCH_TIMEOUT_S = 5.0

# Refresh external rates this often. Hourly is plenty — these series move on
# committee decisions, not minutes.
REFRESH_INTERVAL_S = 60 * 60
# Per-bank fetch status TTLs for the `_source` field on /api/rates.
LIVE_TTL_S = 60 * 60          # within last hour → "live"
STALE_TTL_S = 60 * 60 * 24    # over 24h since last successful fetch → "stale"

# ─── Cache ─────────────────────────────────────────────────────────────────────

_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.Lock()

# Per-bank "last successful live fetch" timestamps (epoch seconds). Used to
# compute the ``_source`` label on /api/rates: live | snapshot_yaml | stale.
_last_live_fetch: dict[str, float] = {}
_last_live_lock = threading.Lock()


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


def _mark_live_fetch(bank_id: str) -> None:
    with _last_live_lock:
        _last_live_fetch[bank_id] = time.time()


def _source_label(bank_id: str) -> str:
    """Classify each bank's current rate provenance for /api/rates.

    Rules:
      - ``live``: a successful upstream fetch within LIVE_TTL_S (1h).
      - ``stale``: a successful fetch happened, but it's older than STALE_TTL_S (24h).
      - ``snapshot_yaml``: no successful fetch on record, or it's between 1h and
        24h old with the cache holding the snapshot.
    """
    with _last_live_lock:
        ts = _last_live_fetch.get(bank_id)
    if ts is None:
        return "snapshot_yaml"
    age = time.time() - ts
    if age <= LIVE_TTL_S:
        return "live"
    if age >= STALE_TTL_S:
        return "stale"
    return "snapshot_yaml"


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


# ─── External fetchers ────────────────────────────────────────────────────────
#
# Each fetcher hits its upstream with a hard 5s timeout, parses the response
# into a small dict, and on any failure logs + returns None so refresh_rates_once
# falls back to the YAML snapshot for that bank. Parsing helpers are split out
# so they can be unit-tested against canned JSON / CSV without touching the
# network.
#
# BoJ has no clean JSON API — we keep it snapshot-only for now.
# TODO(boj): wire BoJ once they publish a stable JSON feed.


def _parse_fred_observation(payload: dict[str, Any]) -> Optional[float]:
    """Pull the numeric value out of a FRED `series/observations` JSON
    response. Returns None on missing / malformed input. Pure function so we
    can unit-test against a canned payload."""
    try:
        obs = payload.get("observations") or []
        if not obs:
            return None
        val = obs[0].get("value")
        if val in (None, "", "."):  # FRED uses "." for missing
            return None
        return float(val)
    except (AttributeError, ValueError, TypeError):
        return None


def _parse_ecb_jsondata(payload: dict[str, Any]) -> Optional[float]:
    """Parse the ECB SDW jsondata shape down to the latest observation value.

    The jsondata format nests observations as
        dataSets[0].series["0:0:0:0:0:0:0"].observations["N"] = [value, ...]
    We grab the highest observation index in the (sole) series.
    """
    try:
        datasets = payload.get("dataSets") or []
        if not datasets:
            return None
        series_map = (datasets[0] or {}).get("series") or {}
        if not series_map:
            return None
        # There's exactly one series key for this single-series query — but
        # iterate defensively just in case the API ever returns more.
        for series in series_map.values():
            observations = (series or {}).get("observations") or {}
            if not observations:
                continue
            # Keys are stringified ints ("0", "1", ...). Take the latest.
            latest_idx = max(observations.keys(), key=lambda k: int(k))
            value = observations[latest_idx][0]
            if value is None:
                return None
            return float(value)
        return None
    except (AttributeError, ValueError, TypeError, KeyError, IndexError):
        return None


def _parse_boe_csv(text: str) -> Optional[float]:
    """Pull the most-recent rate out of the BoE `_iadb-fromshowcolumns.asp`
    CSV response (DATE,VALUE rows after a one-line header)."""
    try:
        rows = [r.strip() for r in text.splitlines() if "," in r]
        if len(rows) < 2:
            return None
        # Skip header, take the last data row, last column (the rate).
        last = rows[-1].split(",")
        if len(last) < 2:
            return None
        return float(last[-1])
    except (ValueError, IndexError):
        return None


async def _fetch_fred_rate(client: httpx.AsyncClient) -> Optional[dict[str, Any]]:
    """Fetch the live Fed funds target band from FRED.

    Series DFEDTARU (upper) + DFEDTARL (lower) → midpoint = rate_pct.
    Requires a FRED API key in $FRED_API_KEY. Without it, return None so the
    caller falls back to the YAML snapshot.
    """
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        logger.info("FRED_API_KEY unset — skipping live Fed rate fetch")
        return None
    base = "https://api.stlouisfed.org/fred/series/observations"

    async def _latest(series: str) -> Optional[float]:
        try:
            r = await client.get(base, params={
                "series_id": series,
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            }, timeout=FETCH_TIMEOUT_S)
        except httpx.HTTPError as e:
            logger.warning("FRED %s fetch failed: %s", series, e)
            return None
        if r.status_code != 200:
            logger.warning("FRED %s returned HTTP %d", series, r.status_code)
            return None
        try:
            return _parse_fred_observation(r.json())
        except ValueError as e:
            logger.warning("FRED %s JSON decode failed: %s", series, e)
            return None

    upper = await _latest("DFEDTARU")
    lower = await _latest("DFEDTARL")
    if upper is None or lower is None:
        return None
    return {
        "band_low_pct": lower,
        "band_high_pct": upper,
        "rate_pct": round((upper + lower) / 2, 4),
        "source": "FRED DFEDTARU / DFEDTARL",
    }


# Backwards-compatible alias — older call sites used the long name.
_fetch_fred_funds_rate = _fetch_fred_rate


async def _fetch_ecb_rate(client: httpx.AsyncClient) -> Optional[dict[str, Any]]:
    """Fetch the live ECB deposit facility rate from the SDW jsondata API.

    Series FM.D.U2.EUR.4F.KR.DFR.LEV — last observation only.
    """
    url = ("https://sdw-wsrest.ecb.europa.eu/service/data/"
           "FM/D.U2.EUR.4F.KR.DFR.LEV")
    try:
        r = await client.get(
            url,
            params={"lastNObservations": "1", "format": "jsondata"},
            headers={"Accept": "application/json"},
            timeout=FETCH_TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        logger.warning("ECB SDW fetch failed: %s", e)
        return None
    if r.status_code != 200:
        logger.warning("ECB SDW returned HTTP %d", r.status_code)
        return None
    try:
        payload = r.json()
    except ValueError as e:
        logger.warning("ECB SDW JSON decode failed: %s", e)
        return None
    rate = _parse_ecb_jsondata(payload)
    if rate is None:
        logger.warning("ECB SDW: no observation in payload")
        return None
    return {
        "rate_pct": round(rate, 4),
        "source": "ECB SDW FM.D.U2.EUR.4F.KR.DFR.LEV",
    }


# Backwards-compatible alias.
_fetch_ecb_deposit_rate = _fetch_ecb_rate


async def _fetch_boe_rate(client: httpx.AsyncClient) -> Optional[dict[str, Any]]:
    """Fetch the live BoE Bank Rate (IUMABEDR) from the BoE IADB CSV endpoint.

    Returns None on any failure so we fall back to the snapshot.
    """
    url = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
    # Use a sliding window — the last 2 years is plenty for the most-recent obs
    # and keeps the response small. Date format is dd/Mon/yyyy.
    datefrom = (date.today() - timedelta(days=730)).strftime("%d/%b/%Y")
    params = {
        "csv.x": "yes",
        "Datefrom": datefrom,
        "Dateto": "now",
        "SeriesCodes": "IUMABEDR",
        "CSVF": "TN",
        "UsingCodes": "Y",
        "Filter": "N",
        "FD": "1",
    }
    try:
        r = await client.get(url, params=params, timeout=FETCH_TIMEOUT_S)
    except httpx.HTTPError as e:
        logger.warning("BoE fetch failed: %s", e)
        return None
    if r.status_code != 200:
        logger.warning("BoE returned HTTP %d", r.status_code)
        return None
    rate = _parse_boe_csv(r.text)
    if rate is None:
        logger.warning("BoE: could not parse rate from CSV response")
        return None
    return {"rate_pct": round(rate, 4), "source": "BoE IUMABEDR"}


# Backwards-compatible alias.
_fetch_boe_bank_rate = _fetch_boe_rate


async def refresh_rates_once() -> dict[str, Any]:
    """Try external APIs, fall back to snapshot. Cache the merged result.

    Each bank fetched successfully has its timestamp recorded so /api/rates
    can tag it ``live`` / ``snapshot_yaml`` / ``stale``.
    """
    snapshot = load_rates_snapshot()
    out: dict[str, Any] = {
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
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            fed, ecb, boe = await asyncio.gather(
                _fetch_fred_rate(client),
                _fetch_ecb_rate(client),
                _fetch_boe_rate(client),
                return_exceptions=True,
            )
            any_live = False
            if isinstance(fed, dict):
                out["banks"].setdefault("fed", {}).update(fed)
                _mark_live_fetch("fed")
                any_live = True
            elif isinstance(fed, Exception):
                logger.warning("FRED fetcher raised: %s", fed)
            if isinstance(ecb, dict):
                out["banks"].setdefault("ecb", {}).update(ecb)
                _mark_live_fetch("ecb")
                any_live = True
            elif isinstance(ecb, Exception):
                logger.warning("ECB fetcher raised: %s", ecb)
            if isinstance(boe, dict):
                out["banks"].setdefault("boe", {}).update(boe)
                _mark_live_fetch("boe")
                any_live = True
            elif isinstance(boe, Exception):
                logger.warning("BoE fetcher raised: %s", boe)
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


# ─── Gateway-SSO auth ─────────────────────────────────────────────────────────
#
# This service is meant to sit behind the narve gateway. Without verifying the
# shared secret, anything that can reach this port can forge identity headers
# and impersonate any subscriber. The middleware below 401s every request
# whose ``X-Gateway-Secret`` doesn't match the server-side secret (constant
# time compare). Combined with binding 127.0.0.1, the dashboard is only
# reachable through the gateway proxy on the same host.

_SSO_SECRET = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
_AUTH_BYPASS_EXACT = {"/health", "/healthz"}

if not _SSO_SECRET and not _DEV_MODE:
    logger.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — every gateway-fronted request will 401.")


@app.middleware("http")
async def gateway_auth(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS":
        return await call_next(request)
    if path in _AUTH_BYPASS_EXACT or path.startswith("/static/"):
        return await call_next(request)
    if _DEV_MODE and not _SSO_SECRET:
        return await call_next(request)
    if not _SSO_SECRET:
        return JSONResponse({"error": "service misconfigured"}, status_code=503)
    client_secret = request.headers.get("x-gateway-secret", "")
    if not hmac.compare_digest(client_secret, _SSO_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


_refresh_task: Optional[asyncio.Task] = None


async def _periodic_refresh() -> None:
    """Hourly refresh: call each fetcher and merge results into the rates cache.

    refresh_rates_once gathers the three fetchers concurrently and records
    per-bank live timestamps via _mark_live_fetch.
    """
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
    # Annotate each bank with a `_source` label so the UI can show provenance.
    # We don't mutate the cache entry — copy on the way out.
    out = dict(cached or {})
    banks: dict[str, Any] = {}
    for bank_id, bank in (cached or {}).get("banks", {}).items():
        bank_copy = dict(bank) if isinstance(bank, dict) else {}
        # BoJ is snapshot-only; we never call a live fetcher for it.
        bank_copy["_source"] = (
            "snapshot_yaml" if bank_id == "boj" else _source_label(bank_id)
        )
        banks[bank_id] = bank_copy
    out["banks"] = banks
    return JSONResponse(out)


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
    # Loopback-only — the gateway is the sole ingress. Override with
    # ``BIND_HOST`` if you need to expose this directly for debugging.
    bind_host = os.environ.get("BIND_HOST", "127.0.0.1")
    logger.info("Starting Central Bank Tracker on %s:%d", bind_host, PORT)
    uvicorn.run("server:app", host=bind_host, port=PORT, reload=False)
