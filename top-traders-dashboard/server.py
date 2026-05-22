#!/usr/bin/env python3
"""
Top Traders Dashboard — tracks the top 3 traders on Polymarket,
streams their recent trades, and scans for suspicious trades.

Data sources (all public, unauthenticated):
  - https://lb-api.polymarket.com/volume?window=<all|1d|7d|30d>&limit=N
      Returns the leaderboard ranked by volume traded in that window.
  - https://data-api.polymarket.com/trades?user=<wallet>&limit=N
      Returns the most recent trades for a given proxy wallet.

Run: python3 server.py   (listens on :8052)
"""

import asyncio
import hmac
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# ── Layered .env loader ──────────────────────────────────────────────────────
# See sports-dashboard for rationale. Walks ~/.gateway_env → gateway/.env.production
# → dashboard/.env.production → dashboard/.env (first definition wins).
try:
    from dotenv import load_dotenv as _dotenv_load
except ImportError:
    def _dotenv_load(p, override=False):
        for raw in Path(p).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if not override and k in os.environ:
                continue
            os.environ[k] = v
        return True
_DASHBOARD_DIR = Path(__file__).resolve().parent
_GATEWAY_ENV = None
for _p in [_DASHBOARD_DIR, *_DASHBOARD_DIR.parents][:5]:
    _candidate = _p / "gateway" / ".env.production"
    if _candidate.is_file():
        _GATEWAY_ENV = _candidate
        break
_ENV_SEARCH = [Path.home() / ".gateway_env"]
if _GATEWAY_ENV is not None:
    _ENV_SEARCH.append(_GATEWAY_ENV)
_ENV_SEARCH.extend([_DASHBOARD_DIR / ".env.production", _DASHBOARD_DIR / ".env"])
_loaded_env_files: list[str] = []
for _f in _ENV_SEARCH:
    if _f.is_file():
        _dotenv_load(_f, override=False)
        _loaded_env_files.append(str(_f))
print(f"[top-traders-dashboard] env files loaded: {len(_loaded_env_files)}", flush=True)
for _f in _loaded_env_files:
    print(f"  ✓ {_f}", flush=True)
for _key, _desc in [
    ("GATEWAY_SSO_SECRET", "gateway-fronted requests will be rejected"),
    ("KALSHI_API_KEY_ID", "Kalshi auth quota (degrades to IP-rate-limited)"),
    ("KALSHI_PRIVATE_KEY", "Kalshi auth signing"),
]:
    if not os.getenv(_key):
        print(f"⚠ [top-traders-dashboard] {_key} missing — {_desc}", flush=True)

# ─── Config ───────────────────────────────────────────────────────────
PORT = 8052
LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
ALLOWED_WINDOWS = {"all", "1d", "7d", "30d"}
CACHE_TTL_SECONDS = 20  # shorter than the 30s frontend poll to avoid serving stale data
HTTP_TIMEOUT = 15.0

HERE = Path(__file__).parent
INDEX_HTML = HERE / "index.html"
FAVICON_PNG = HERE / "favicon.png"

app = FastAPI(title="Polymarket Top Traders Dashboard")

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret:
    if _DEV_MODE:
        logging.warning("GATEWAY_SSO_SECRET not set — top-traders dashboard running in DEV_MODE (no auth)")
    else:
        logging.warning("GATEWAY_SSO_SECRET not set and DEV_MODE not enabled — rejecting all requests")


@app.middleware("http")
async def gateway_auth_middleware(request: Request, call_next):
    """Verify gateway SSO secret on all requests. Reject if secret not configured (unless DEV_MODE)."""
    if _sso_secret:
        client_secret = request.headers.get("x-gateway-secret", "")
        if not hmac.compare_digest(client_secret, _sso_secret):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    elif not _DEV_MODE:
        return JSONResponse({"error": "Service misconfigured"}, status_code=503)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response

# Small in-memory LRU cache: { key -> (expires_at, payload) }
# Previously this was a plain dict that would `.clear()` itself when full,
# producing periodic thundering-herd cache misses against the upstream API.
# OrderedDict + move_to_end + popitem(last=False) gives us proper LRU
# semantics so the eviction is gradual and old hot keys stay resident.
_CACHE_MAX_SIZE = 100
_cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and entry[0] > time.time():
        # Touch this entry so it counts as "recent" for LRU eviction.
        _cache.move_to_end(key)
        return entry[1]
    if entry:
        # Expired — drop it so size accounting stays accurate.
        _cache.pop(key, None)
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.time() + CACHE_TTL_SECONDS, value)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX_SIZE:
        _cache.popitem(last=False)


async def _fetch_json(client: httpx.AsyncClient, url: str, params: dict) -> Any:
    r = await client.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ─── FX rates proxy (frankfurter.dev) ─────────────────────────────────
_FX_CACHE: dict = {"data": None, "fetched_at": 0.0}
_FX_TTL = 3600  # 1 hour
_FX_FALLBACK = {
    "base": "USD",
    "date": "fallback",
    "rates": {
        "USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 150.0, "AUD": 1.52,
        "CAD": 1.36, "CHF": 0.88, "CNY": 7.20, "HKD": 7.83, "NZD": 1.65,
        "SEK": 10.5, "KRW": 1340.0, "SGD": 1.34, "NOK": 10.6, "MXN": 17.0,
        "INR": 83.0, "ZAR": 18.5, "TRY": 32.0, "BRL": 5.0, "DKK": 6.85,
        "PLN": 3.95, "THB": 35.0, "IDR": 15700.0, "HUF": 360.0, "CZK": 23.0,
        "ILS": 3.7, "PHP": 56.0, "MYR": 4.7, "RON": 4.6, "ISK": 137.0,
    },
}


@app.get("/api/fx-rates")
async def fx_rates():
    """USD-base FX rates, cached for 1h. Source: frankfurter.dev."""
    now = time.time()
    cached = _FX_CACHE["data"]
    if cached and (now - _FX_CACHE["fetched_at"]) < _FX_TTL:
        return cached
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://api.frankfurter.dev/v1/latest?base=USD")
            if r.status_code == 200:
                data = r.json()
                data.setdefault("rates", {})
                data["rates"]["USD"] = 1.0
                _FX_CACHE["data"] = data
                _FX_CACHE["fetched_at"] = now
                return data
    except Exception as e:
        logging.warning("FX rate fetch failed: %s", e)
    if cached:
        return cached
    return _FX_FALLBACK


# ─── API routes ───────────────────────────────────────────────────────
@app.get("/api/leaderboard")
async def leaderboard(
    window: str = Query("all"),
    limit: int = Query(3, ge=1, le=25),
):
    """Top N traders by volume for the given window."""
    if window not in ALLOWED_WINDOWS:
        raise HTTPException(400, f"window must be one of {sorted(ALLOWED_WINDOWS)}")

    key = f"lb:{window}:{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient() as client:
        try:
            data = await _fetch_json(
                client, f"{LB_API}/volume", {"window": window, "limit": limit}
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Polymarket leaderboard fetch failed: {e}")

    _cache_set(key, data)
    return data


@app.get("/api/trades")
async def user_trades(
    user: str = Query(..., min_length=10, max_length=42, pattern=r"^0x[a-fA-F0-9]{40}$"),
    limit: int = Query(25, ge=1, le=500),
):
    """Recent trades for a specific wallet."""
    key = f"tr:{user.lower()}:{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient() as client:
        try:
            data = await _fetch_json(
                client, f"{DATA_API}/trades", {"user": user, "limit": limit}
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Polymarket trades fetch failed: {e}")

    _cache_set(key, data)
    return data


@app.get("/api/top-traders")
async def top_traders(
    window: str = Query("all"),
    trades_per_trader: int = Query(25, ge=1, le=100),
    rank: str = Query("volume", pattern=r"^(volume|profit)$"),
):
    """
    Convenience endpoint: returns the top 3 traders and each trader's recent
    trades in a single call, so the frontend only makes one fetch.

    rank=volume → leaderboard by traded notional
    rank=profit → leaderboard by realized PnL (the 'who's actually winning' view)
    """
    if window not in ALLOWED_WINDOWS:
        raise HTTPException(400, f"window must be one of {sorted(ALLOWED_WINDOWS)}")

    key = f"top3:{window}:{trades_per_trader}:{rank}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    lb_path = "/profit" if rank == "profit" else "/volume"

    async with httpx.AsyncClient() as client:
        try:
            lb = await _fetch_json(
                client, f"{LB_API}{lb_path}", {"window": window, "limit": 3}
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Polymarket leaderboard fetch failed: {e}")

        traders = []
        for r, entry in enumerate(lb, start=1):
            wallet = entry.get("proxyWallet")
            if not wallet:
                continue
            try:
                trades = await _fetch_json(
                    client,
                    f"{DATA_API}/trades",
                    {"user": wallet, "limit": trades_per_trader},
                )
            except httpx.HTTPError:
                trades = []
            # `amount` is the metric for whichever leaderboard we hit
            metric = entry.get("amount", 0)
            traders.append(
                {
                    "rank": r,
                    "proxyWallet": wallet,
                    "name": entry.get("name") or entry.get("pseudonym") or wallet[:10],
                    "pseudonym": entry.get("pseudonym"),
                    "volume": metric if rank == "volume" else 0,
                    "profit": metric if rank == "profit" else 0,
                    "metric_label": "PnL" if rank == "profit" else "volume",
                    "profileImage": entry.get("profileImageOptimized")
                    or entry.get("profileImage")
                    or "",
                    "bio": entry.get("bio", ""),
                    "trades": trades,
                }
            )

    payload = {
        "window": window,
        "rank_by": rank,
        "fetched_at": int(time.time()),
        "traders": traders,
    }
    _cache_set(key, payload)
    return payload


# ─── Suspicious Trades Scanner ───────────────────────────────────────
_last_sus_scan: dict = {}
_last_sus_scan_time: float = 0
SUS_CACHE_TTL = 1800  # serve cached data for 30 min
_bg_tasks: set = set()  # prevent GC of background tasks


async def _suspicious_trade_monitor():
    """Periodically scan for suspicious trades in the background."""
    global _last_sus_scan, _last_sus_scan_time
    from suspicious_trades import run_scanner

    await asyncio.sleep(10)  # let server start

    while True:
        try:
            result = await asyncio.to_thread(run_scanner)
            if result:
                _last_sus_scan = result
                _last_sus_scan_time = time.time()
                logging.info(
                    "Suspicious scan complete: %d flagged trades",
                    len(result.get("suspicious_trades", [])),
                )
        except Exception as e:
            logging.error("Suspicious scanner error: %s", e)

        await asyncio.sleep(1800)  # re-scan every 30 min


@app.on_event("startup")
async def _start_scanner():
    task = asyncio.create_task(_suspicious_trade_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ─── Insider events (SEC Form 4 + future congress/13F) ──────────────
_last_form4_ingest: dict = {}
_last_form4_ts: float = 0
FORM4_POLL_INTERVAL = 900   # 15 min — EDGAR atom feed cadence


async def _form4_monitor():
    """Periodically poll EDGAR Form 4 atom feed and land into insider_events."""
    global _last_form4_ingest, _last_form4_ts
    try:
        from edgar_form4 import run_ingest, is_available
    except ImportError as e:
        logging.warning("edgar_form4 module unavailable: %s", e)
        return

    await asyncio.sleep(15)  # let server start, stagger from suspicious scanner

    while True:
        if not is_available():
            # No SEC_USER_AGENT set — skip silently and re-check in 30 min
            await asyncio.sleep(1800)
            continue
        try:
            result = await asyncio.to_thread(run_ingest)
            _last_form4_ingest = result
            _last_form4_ts = time.time()
            logging.info(
                "EDGAR Form 4 ingest: parsed=%d inserted=%d skipped=%d errors=%d",
                result.get("filings_parsed", 0),
                result.get("inserted", 0),
                result.get("skipped", 0),
                result.get("errors", 0),
            )
        except Exception as e:
            logging.error("EDGAR Form 4 ingest error: %s", e)
        await asyncio.sleep(FORM4_POLL_INTERVAL)


@app.on_event("startup")
async def _start_form4_monitor():
    task = asyncio.create_task(_form4_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ─── Congress PTR poller ────────────────────────────────────────────
_last_ptr_ingest: dict = {}
_last_ptr_ts: float = 0
PTR_POLL_INTERVAL = 6 * 3600              # 6h — source refreshes daily
PTR_BACKFILL_DAYS_AT_BOOT = 365          # first run keeps the last year
PTR_BACKFILL_DAYS_STEADY = 60            # subsequent runs stay light


async def _congress_ptr_monitor():
    global _last_ptr_ingest, _last_ptr_ts
    try:
        from congress_ptr import run_ingest
    except ImportError as e:
        logging.warning("congress_ptr module unavailable: %s", e)
        return

    await asyncio.sleep(30)  # stagger from form4 + suspicious scanner
    first = True
    while True:
        days = PTR_BACKFILL_DAYS_AT_BOOT if first else PTR_BACKFILL_DAYS_STEADY
        try:
            result = await asyncio.to_thread(run_ingest, only_since_filed_days=days)
            _last_ptr_ingest = result
            _last_ptr_ts = time.time()
            h = result.get("house") or {}
            s = result.get("senate") or {}
            logging.info(
                "Congress PTR ingest (last %dd): house inserted=%d, senate inserted=%d",
                days, h.get("inserted", 0), s.get("inserted", 0),
            )
        except Exception as e:
            logging.error("Congress PTR ingest error: %s", e)
        first = False
        await asyncio.sleep(PTR_POLL_INTERVAL)


@app.on_event("startup")
async def _start_ptr_monitor():
    task = asyncio.create_task(_congress_ptr_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@app.get("/api/congress-ptr/status")
async def congress_ptr_status():
    """Last PTR ingest result + timestamp (debugging)."""
    return {
        "ran_at": int(_last_ptr_ts) if _last_ptr_ts else None,
        "result": _last_ptr_ingest or None,
        "enrichment": {
            "ran_at": int(_last_ptr_enrich_ts) if _last_ptr_enrich_ts else None,
            "result": _last_ptr_enrich or None,
        },
    }


# ─── PTR PDF enrichment (House) ─────────────────────────────────────
# The House Clerk XML index gives us "Member X filed PTR Y on date Z" but no
# ticker/side/amount — those live inside the PDF. This poller walks unenriched
# filing rows, fetches each PDF, and replaces the parent row with N
# transaction-level detail rows. Cadence is intentionally slow (30 min) and
# capped (30 filings/pass) so we don't hammer disclosures-clerk.house.gov.
_last_ptr_enrich: dict = {}
_last_ptr_enrich_ts: float = 0
PTR_ENRICH_INTERVAL = 1800  # 30 min


async def _ptr_enrich_monitor():
    global _last_ptr_enrich, _last_ptr_enrich_ts
    try:
        from congress_ptr import enrich_house_filings
    except ImportError as e:
        logging.warning("congress_ptr enrich unavailable: %s", e)
        return

    # Wait for the initial PTR ingest to land at least one batch of filing rows.
    await asyncio.sleep(180)
    while True:
        try:
            result = await asyncio.to_thread(enrich_house_filings, max_filings=30)
            _last_ptr_enrich = result
            _last_ptr_enrich_ts = time.time()
            if result.get("txs_written", 0) > 0:
                logging.info(
                    "PTR enrich: filings=%d parsed=%d txs=%d failures=%d",
                    result.get("filings_seen", 0),
                    result.get("parsed", 0),
                    result.get("txs_written", 0),
                    result.get("parse_failures", 0) + result.get("fetch_failures", 0),
                )
        except Exception as e:
            logging.error("PTR enrich error: %s", e)
        await asyncio.sleep(PTR_ENRICH_INTERVAL)


@app.on_event("startup")
async def _start_ptr_enrich_monitor():
    task = asyncio.create_task(_ptr_enrich_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ─── Cross-venue correlation engine ─────────────────────────────────
_last_corr_pass: dict = {}
_last_corr_ts: float = 0
CORR_POLL_INTERVAL = 3600  # 1h — slower than ingesters; price history is heavy


async def _correlation_monitor():
    global _last_corr_pass, _last_corr_ts
    try:
        from correlation import run_correlation_pass
    except ImportError as e:
        logging.warning("correlation module unavailable: %s", e)
        return

    # Wait long enough for the first ingest passes to land some events,
    # otherwise the very first correlation pass has nothing to chew on.
    await asyncio.sleep(120)
    while True:
        try:
            result = await asyncio.to_thread(run_correlation_pass, 100)
            _last_corr_pass = result
            _last_corr_ts = time.time()
            logging.info(
                "Correlation pass: processed=%d, with_matches=%d, inserted=%d",
                result.get("events_processed", 0),
                result.get("events_with_matches", 0),
                result.get("rows_inserted", 0),
            )
        except Exception as e:
            logging.error("Correlation pass error: %s", e)
        await asyncio.sleep(CORR_POLL_INTERVAL)


@app.on_event("startup")
async def _start_correlation_monitor():
    task = asyncio.create_task(_correlation_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@app.get("/api/insider-correlations")
async def insider_correlations_api(
    min_abs_delta: float = Query(0.05, ge=0.0, le=1.0),
    venue: str | None = Query(None, pattern=r"^(sec_form4|congress_ptr|13f|polymarket|kalshi)$"),
    limit: int = Query(50, ge=1, le=200),
):
    """Cross-venue insider feed — biggest pre-disclosure Polymarket moves first."""
    try:
        from correlation import top_correlations, correlations_summary
    except ImportError:
        raise HTTPException(503, "correlation engine unavailable")
    return {
        "correlations": top_correlations(
            min_abs_delta=min_abs_delta, venue=venue, limit=limit,
        ),
        "summary": correlations_summary(),
        "last_pass": {
            "ran_at": int(_last_corr_ts) if _last_corr_ts else None,
            "result": _last_corr_pass or None,
        },
    }


@app.get("/api/ticker-market-index")
async def ticker_market_index_api():
    """Snapshot of the ticker → Polymarket market mapping (debugging the engine)."""
    try:
        from ticker_to_market import index_summary, get_index
    except ImportError:
        raise HTTPException(503, "ticker_to_market module unavailable")
    get_index()  # ensure populated
    return index_summary()


# ─── Polymarket → insider_events bridge ─────────────────────────────
_last_bridge_pass: dict = {}
_last_bridge_ts: float = 0
BRIDGE_POLL_INTERVAL = 1800  # 30 min — same cadence as suspicious scanner


async def _polymarket_bridge_monitor():
    """Project the latest sus-scan into insider_events with venue='polymarket'."""
    global _last_bridge_pass, _last_bridge_ts
    try:
        from polymarket_bridge import run_bridge, import_leaderboard_pseudonyms
    except ImportError as e:
        logging.warning("polymarket_bridge unavailable: %s", e)
        return

    # Wait for the suspicious scanner to land its first scan, then run.
    await asyncio.sleep(60)
    while True:
        try:
            # Auto-import any new leaderboard pseudonyms first — these become
            # display names for the bridged rows and for the existing trader
            # tabs.
            try:
                async with httpx.AsyncClient() as client:
                    lb = await _fetch_json(client, f"{LB_API}/volume",
                                           {"window": "30d", "limit": 50})
                if isinstance(lb, list):
                    await asyncio.to_thread(import_leaderboard_pseudonyms, lb)
            except Exception as e:
                logging.debug("pseudonym import skipped: %s", e)

            scan = _last_sus_scan or None
            result = await asyncio.to_thread(run_bridge, scan)
            _last_bridge_pass = result
            _last_bridge_ts = time.time()
            logging.info(
                "PM bridge: built=%d inserted=%d skipped=%d",
                result.get("rows_built", 0),
                result.get("inserted", 0),
                result.get("skipped", 0),
            )
        except Exception as e:
            logging.error("PM bridge error: %s", e)
        await asyncio.sleep(BRIDGE_POLL_INTERVAL)


@app.on_event("startup")
async def _start_pm_bridge():
    task = asyncio.create_task(_polymarket_bridge_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ─── Kalshi unusual-prints ingester ─────────────────────────────────
_last_kalshi_unusual: dict = {}
_last_kalshi_unusual_ts: float = 0
KALSHI_UNUSUAL_INTERVAL = 1800  # 30 min — public trades feed, polite cadence


async def _kalshi_unusual_monitor():
    """Scan top Kalshi markets for outsized prints and land into insider_events."""
    global _last_kalshi_unusual, _last_kalshi_unusual_ts
    try:
        from kalshi_unusual_prints import run_ingest
    except ImportError as e:
        logging.warning("kalshi_unusual_prints unavailable: %s", e)
        return

    await asyncio.sleep(45)  # stagger after the other ingesters
    while True:
        try:
            result = await asyncio.to_thread(run_ingest, 80)
            _last_kalshi_unusual = result
            _last_kalshi_unusual_ts = time.time()
            logging.info(
                "Kalshi unusual prints: scanned=%d flagged=%d inserted=%d",
                result.get("markets_scanned", 0),
                result.get("trades_flagged", 0),
                result.get("inserted", 0),
            )
        except Exception as e:
            logging.error("Kalshi unusual prints error: %s", e)
        await asyncio.sleep(KALSHI_UNUSUAL_INTERVAL)


@app.on_event("startup")
async def _start_kalshi_unusual_monitor():
    task = asyncio.create_task(_kalshi_unusual_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@app.get("/api/kalshi/unusual-prints/status")
async def kalshi_unusual_status():
    return {
        "ran_at": int(_last_kalshi_unusual_ts) if _last_kalshi_unusual_ts else None,
        "result": _last_kalshi_unusual or None,
    }


# ─── Watchlist + alert inbox ────────────────────────────────────────
_last_alerts_pass: dict = {}
_last_alerts_ts: float = 0
ALERTS_POLL_INTERVAL = 60  # 1 min — cheap; just walks new event ids


async def _alerts_monitor():
    """Fan out new insider_events to subscribed users' inboxes."""
    global _last_alerts_pass, _last_alerts_ts
    try:
        from watchlist import process_new_events
    except ImportError as e:
        logging.warning("watchlist module unavailable: %s", e)
        return

    await asyncio.sleep(90)  # let ingesters land at least one batch
    while True:
        try:
            result = await asyncio.to_thread(process_new_events)
            _last_alerts_pass = result
            _last_alerts_ts = time.time()
            if result.get("alerts_created", 0) > 0:
                logging.info(
                    "Alerts pass: scanned=%d created=%d dispatched=%d",
                    result.get("events_scanned", 0),
                    result.get("alerts_created", 0),
                    result.get("dispatched", 0),
                )
        except Exception as e:
            logging.error("Alerts pass error: %s", e)
        await asyncio.sleep(ALERTS_POLL_INTERVAL)


@app.on_event("startup")
async def _start_alerts_monitor():
    task = asyncio.create_task(_alerts_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _watchlist_user_id(request: Request) -> str:
    """Same identity convention as Kalshi: gateway header or DEV_MODE 'default'."""
    if _DEV_MODE and not _sso_secret:
        return "default"
    uid = request.headers.get("x-user-id")
    if not uid:
        raise HTTPException(400, "Missing x-user-id header")
    return uid


@app.get("/api/watchlist")
async def watchlist_list(request: Request):
    """List the calling user's watched actors."""
    try:
        from watchlist import list_watches, status_summary
    except ImportError:
        raise HTTPException(503, "watchlist module unavailable")
    user_id = _watchlist_user_id(request)
    return {
        "watches": await asyncio.to_thread(list_watches, user_id),
        "summary": await asyncio.to_thread(status_summary, user_id),
    }


@app.post("/api/watchlist")
async def watchlist_add(request: Request):
    """
    Body: { "actor_id": "house:pelosi-nancy", "label": "Nancy Pelosi" }
    actor_id should match insider_events.actor_id (CIK, congressperson slug,
    wallet address — see each ingester for the exact format it produces).
    """
    try:
        from watchlist import add_watch
    except ImportError:
        raise HTTPException(503, "watchlist module unavailable")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON")
    actor = (body.get("actor_id") or "").strip()
    label = (body.get("label") or "").strip() or None
    if not actor:
        raise HTTPException(400, "actor_id is required")
    user_id = _watchlist_user_id(request)
    try:
        added = await asyncio.to_thread(add_watch, user_id, actor, label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "added": added}


@app.delete("/api/watchlist")
async def watchlist_remove(
    request: Request,
    actor_id: str = Query(..., min_length=1, max_length=200),
):
    try:
        from watchlist import remove_watch
    except ImportError:
        raise HTTPException(503, "watchlist module unavailable")
    user_id = _watchlist_user_id(request)
    removed = await asyncio.to_thread(remove_watch, user_id, actor_id)
    return {"ok": True, "removed": removed}


@app.get("/api/watchlist/inbox")
async def watchlist_inbox(
    request: Request,
    unread_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
):
    """Recent alerts for the calling user, joined with the source event."""
    try:
        from watchlist import list_inbox, unread_count
    except ImportError:
        raise HTTPException(503, "watchlist module unavailable")
    user_id = _watchlist_user_id(request)
    items = await asyncio.to_thread(
        list_inbox, user_id, unread_only=unread_only, limit=limit,
    )
    unread = await asyncio.to_thread(unread_count, user_id)
    return {
        "inbox": items,
        "unread_count": unread,
        "last_pass": {
            "ran_at": int(_last_alerts_ts) if _last_alerts_ts else None,
            "result": _last_alerts_pass or None,
        },
    }


@app.post("/api/watchlist/inbox/read")
async def watchlist_mark_read(request: Request):
    """
    Body: { "inbox_ids": [1,2,3] }  (or {} to mark all read)
    """
    try:
        from watchlist import mark_read, mark_all_read
    except ImportError:
        raise HTTPException(503, "watchlist module unavailable")
    try:
        body = await request.json()
    except Exception:
        body = {}
    user_id = _watchlist_user_id(request)
    ids = body.get("inbox_ids") if isinstance(body, dict) else None
    if ids:
        try:
            ids = [int(i) for i in ids]
        except Exception:
            raise HTTPException(400, "inbox_ids must be a list of ints")
        n = await asyncio.to_thread(mark_read, user_id, ids)
    else:
        n = await asyncio.to_thread(mark_all_read, user_id)
    return {"ok": True, "marked": n}


# ─── One-shot correlation backfill ──────────────────────────────────
_last_backfill: dict = {}
_last_backfill_ts: float = 0
_backfill_in_progress: bool = False


async def _do_backfill(max_events: int) -> dict:
    """Run a large correlation pass off the event loop. Set the in-progress flag."""
    global _last_backfill, _last_backfill_ts, _backfill_in_progress
    try:
        from correlation import run_correlation_pass
    except ImportError:
        return {"ok": False, "reason": "correlation module unavailable"}
    _backfill_in_progress = True
    try:
        result = await asyncio.to_thread(run_correlation_pass, max_events)
        _last_backfill = result
        _last_backfill_ts = time.time()
        logging.info("Correlation backfill done: %s", result)
        return {"ok": True, **result}
    except Exception as e:
        logging.error("Correlation backfill error: %s", e)
        return {"ok": False, "reason": str(e)}
    finally:
        _backfill_in_progress = False


@app.post("/api/correlation/backfill")
async def correlation_backfill(
    max_events: int = Query(2000, ge=1, le=20000),
    fire_and_forget: bool = Query(True),
):
    """
    Run a one-shot correlation pass over historical events. Defaults to
    fire-and-forget (returns immediately, runs in background) because a
    20k-event pass can take 30+ minutes due to upstream rate limits on
    the CLOB price-history API.
    """
    if _backfill_in_progress:
        return {"ok": False, "reason": "backfill already running",
                "last_result": _last_backfill or None}

    if fire_and_forget:
        task = asyncio.create_task(_do_backfill(max_events))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return {"ok": True, "started": True, "max_events": max_events}

    result = await _do_backfill(max_events)
    return result


@app.get("/api/correlation/backfill/status")
async def correlation_backfill_status():
    return {
        "in_progress": _backfill_in_progress,
        "last_ran_at": int(_last_backfill_ts) if _last_backfill_ts else None,
        "last_result": _last_backfill or None,
    }


# ─── Boot-time backfill: kick off once after the first PTR ingest lands
_BOOT_BACKFILL_DELAY = 30 * 60   # 30 min — give Form 4 + PTR enough time to settle
_BOOT_BACKFILL_BUDGET = 5000     # cap on first pass; user can re-trigger via API


async def _boot_backfill():
    """One-shot at startup: backfill correlations once history has been imported."""
    await asyncio.sleep(_BOOT_BACKFILL_DELAY)
    if _backfill_in_progress:
        return
    logging.info("Starting boot-time correlation backfill (max_events=%d)",
                 _BOOT_BACKFILL_BUDGET)
    await _do_backfill(_BOOT_BACKFILL_BUDGET)


@app.on_event("startup")
async def _start_boot_backfill():
    task = asyncio.create_task(_boot_backfill())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ─── SEC enforcement archive (Litigation Releases) ──────────────────
# Two-pass ingester: pull the LR index, then enrich detail bodies for
# insider-trading classification + defendant→actor matching.
_last_sec_lit_index: dict = {}
_last_sec_lit_enrich: dict = {}
_last_sec_lit_match: dict = {}
_last_sec_lit_ts: float = 0
SEC_LIT_INDEX_INTERVAL = 3600       # 1h — newest releases
SEC_LIT_DETAIL_INTERVAL = 1800      # 30 min — enrich bodies
SEC_LIT_MATCH_INTERVAL = 1800       # 30 min — refresh defendant→actor links


async def _sec_litigation_monitor():
    """Three sub-passes on staggered cadences. All cheap once the archive
    is bootstrapped (incremental from there on)."""
    global _last_sec_lit_index, _last_sec_lit_enrich, _last_sec_lit_match, _last_sec_lit_ts
    try:
        from sec_litigation import (
            run_index_ingest, run_detail_enrich, MAX_PAGES_AT_BOOT, is_available,
        )
        from enforcement_match import run_match_pass
    except ImportError as e:
        logging.warning("sec_litigation unavailable: %s", e)
        return

    await asyncio.sleep(60)
    first = True
    while True:
        if not is_available():
            logging.debug("SEC_USER_AGENT not set — SEC litigation skipped")
            await asyncio.sleep(1800)
            continue
        try:
            pages = MAX_PAGES_AT_BOOT if first else 2
            res_idx = await asyncio.to_thread(run_index_ingest, max_pages=pages)
            _last_sec_lit_index = res_idx
            logging.info("SEC LR index: pages=%d rows=%d inserted=%d",
                         res_idx.get("pages_seen", 0),
                         res_idx.get("rows_seen", 0),
                         res_idx.get("inserted", 0))
        except Exception as e:
            logging.error("SEC LR index error: %s", e)

        try:
            res_enr = await asyncio.to_thread(run_detail_enrich, max_cases=40)
            _last_sec_lit_enrich = res_enr
            if res_enr.get("flagged_insider", 0) > 0:
                logging.info("SEC LR enrich: fetched=%d flagged_insider=%d",
                             res_enr.get("fetched", 0),
                             res_enr.get("flagged_insider", 0))
        except Exception as e:
            logging.error("SEC LR enrich error: %s", e)

        try:
            res_m = await asyncio.to_thread(run_match_pass)
            _last_sec_lit_match = res_m
            if res_m.get("links_created", 0) > 0:
                logging.info("Defendant→actor match: created=%d (cases=%d)",
                             res_m.get("links_created", 0),
                             res_m.get("cases_seen", 0))
        except Exception as e:
            logging.error("Defendant match error: %s", e)

        _last_sec_lit_ts = time.time()
        first = False
        await asyncio.sleep(SEC_LIT_INDEX_INTERVAL)


@app.on_event("startup")
async def _start_sec_litigation_monitor():
    task = asyncio.create_task(_sec_litigation_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@app.get("/api/enforcement/cases")
async def enforcement_cases_api(
    insider_only: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
):
    try:
        from sec_litigation import recent_cases, stats_summary
    except ImportError:
        raise HTTPException(503, "sec_litigation module unavailable")
    return {
        "cases": await asyncio.to_thread(recent_cases, insider_only=insider_only, limit=limit),
        "summary": await asyncio.to_thread(stats_summary),
        "last_pass": {
            "ran_at": int(_last_sec_lit_ts) if _last_sec_lit_ts else None,
            "index": _last_sec_lit_index or None,
            "enrich": _last_sec_lit_enrich or None,
            "match": _last_sec_lit_match or None,
        },
    }


@app.get("/api/enforcement/active-defendants")
async def active_defendants_api(
    since_days: int = Query(540, ge=30, le=3650),
    limit: int = Query(100, ge=1, le=500),
):
    """Defendants who got busted AND are still actively trading.

    A row per (case, actor) pair where the linked actor has filed an
    event in the last `since_days`. The headline answer to "who's still
    out there after being charged with insider trading?".
    """
    try:
        from enforcement_match import active_defendants, match_summary
    except ImportError:
        raise HTTPException(503, "enforcement_match unavailable")
    return {
        "defendants": await asyncio.to_thread(
            active_defendants, since_days=since_days, limit=limit,
        ),
        "summary": await asyncio.to_thread(match_summary),
    }


# ─── Cross-venue suspicious-trades scoring ──────────────────────────
# Re-scores all events in the lookback window every 30 min using six
# composable heuristics (see cross_venue_suspicious.py docstring).
_last_csv_pass: dict = {}
_last_csv_ts: float = 0
CSV_INTERVAL = 1800


async def _cross_venue_suspicious_monitor():
    global _last_csv_pass, _last_csv_ts
    try:
        from cross_venue_suspicious import refresh_scores
    except ImportError as e:
        logging.warning("cross_venue_suspicious unavailable: %s", e)
        return
    # Wait until the first round of ingest + correlation has landed
    await asyncio.sleep(360)
    while True:
        try:
            result = await asyncio.to_thread(refresh_scores)
            _last_csv_pass = result
            _last_csv_ts = time.time()
            if result.get("nonzero_scores", 0) > 0:
                logging.info(
                    "Cross-venue suspicious: scored=%d nonzero=%d",
                    result.get("scored", 0),
                    result.get("nonzero_scores", 0),
                )
        except Exception as e:
            logging.error("Cross-venue suspicious error: %s", e)
        await asyncio.sleep(CSV_INTERVAL)


@app.on_event("startup")
async def _start_cross_venue_suspicious_monitor():
    task = asyncio.create_task(_cross_venue_suspicious_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@app.get("/api/cross-venue-suspicious")
async def cross_venue_suspicious_api(
    min_score: int = Query(30, ge=0, le=100),
    venue: str | None = Query(None, pattern=r"^(sec_form4|congress_ptr|13f|polymarket|kalshi)$"),
    limit: int = Query(50, ge=1, le=500),
):
    try:
        from cross_venue_suspicious import top_suspicious_events, stats_summary
    except ImportError:
        raise HTTPException(503, "cross_venue_suspicious unavailable")
    return {
        "events": await asyncio.to_thread(
            top_suspicious_events, min_score=min_score, venue=venue, limit=limit,
        ),
        "summary": await asyncio.to_thread(stats_summary),
        "last_pass": {
            "ran_at": int(_last_csv_ts) if _last_csv_ts else None,
            "result": _last_csv_pass or None,
        },
    }


# ─── Actor leakage scoring ──────────────────────────────────────────
# Materialised "who actually has an edge?" view: per-actor average |Δ_pre|
# weighted by sample size, percentile-ranked within actors with ≥3 matches.
# Recompute every 30 min — it's pure SQL, costs <1s even at 100k events.
_last_actor_scores: dict = {}
_last_actor_scores_ts: float = 0
ACTOR_SCORES_INTERVAL = 1800  # 30 min


async def _actor_scores_monitor():
    global _last_actor_scores, _last_actor_scores_ts
    try:
        from actor_scores import refresh_actor_scores
    except ImportError as e:
        logging.warning("actor_scores unavailable: %s", e)
        return
    # Wait for the first correlation pass to land before computing — otherwise
    # the score table is just zeroes for everyone.
    await asyncio.sleep(240)
    while True:
        try:
            result = await asyncio.to_thread(refresh_actor_scores)
            _last_actor_scores = result
            _last_actor_scores_ts = time.time()
            logging.info(
                "Actor scores refreshed: scored=%d with_matches=%d qualifying=%d",
                result.get("actors_scored", 0),
                result.get("with_matches", 0),
                result.get("qualifying_for_percentile", 0),
            )
        except Exception as e:
            logging.error("Actor scores refresh error: %s", e)
        await asyncio.sleep(ACTOR_SCORES_INTERVAL)


@app.on_event("startup")
async def _start_actor_scores_monitor():
    task = asyncio.create_task(_actor_scores_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@app.get("/api/actor-leaderboard")
async def actor_leaderboard(
    venue: str | None = Query(None, pattern=r"^(sec_form4|congress_ptr|13f|polymarket|kalshi)$"),
    min_matches: int = Query(3, ge=1, le=100),
    limit: int = Query(50, ge=1, le=500),
):
    """Ranked list of actors by leakage score (avg |Δ_pre| × ln(1+matches))."""
    try:
        from actor_scores import top_actors_by_leakage, scores_summary
    except ImportError:
        raise HTTPException(503, "actor_scores module unavailable")
    return {
        "actors": await asyncio.to_thread(
            top_actors_by_leakage, venue=venue, min_matches=min_matches, limit=limit,
        ),
        "summary": await asyncio.to_thread(scores_summary),
        "last_refresh": {
            "ran_at": int(_last_actor_scores_ts) if _last_actor_scores_ts else None,
            "result": _last_actor_scores or None,
        },
    }


@app.get("/api/actor-profile")
async def actor_profile(
    actor_id: str = Query(..., min_length=1, max_length=120),
    event_limit: int = Query(50, ge=1, le=500),
):
    """
    Full profile page for one actor: their score + recent events +
    their cross-venue correlation rows. Fan-in from three modules.
    """
    try:
        from actor_scores import get_actor_score
        from insider_events import events_for_actor
    except ImportError:
        raise HTTPException(503, "required modules unavailable")
    score = await asyncio.to_thread(get_actor_score, actor_id)
    events = await asyncio.to_thread(events_for_actor, actor_id, event_limit)

    # Pull this actor's correlation rows for the "biggest pre-disclosure moves" panel
    correlations: list[dict] = []
    try:
        import sqlite3
        from pathlib import Path
        with sqlite3.connect(Path(__file__).parent / "insider_events.db") as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """
                SELECT c.*, e.actor_label, e.actor_role, e.side, e.size_usd_low, e.size_usd_high
                FROM insider_market_correlations c
                JOIN insider_events e ON e.id = c.event_id
                WHERE e.actor_id = ?
                  AND c.delta_pre IS NOT NULL
                ORDER BY ABS(c.delta_pre) DESC
                LIMIT 50
                """,
                (actor_id,),
            ).fetchall()
            correlations = [dict(r) for r in rows]
    except Exception as e:
        logging.debug("actor-profile correlations fetch failed: %s", e)

    if not score and not events:
        raise HTTPException(404, f"actor not found: {actor_id}")

    # Enforcement history (best-effort)
    enforcement_history: list[dict] = []
    try:
        from enforcement_match import enforcement_for_actor
        enforcement_history = await asyncio.to_thread(enforcement_for_actor, actor_id)
    except Exception:
        pass

    # Suspicion scores for this actor's recent events (decorate event list)
    suspicion_by_event: dict[int, dict] = {}
    try:
        import sqlite3 as _sql
        from pathlib import Path as _P
        with _sql.connect(_P(__file__).parent / "insider_events.db") as conn:
            conn.row_factory = _sql.Row
            ev_ids = [e.get("id") for e in events if e.get("id")]
            if ev_ids:
                placeholders = ",".join("?" for _ in ev_ids)
                rs = conn.execute(
                    f"SELECT event_id, score, reasons_json FROM cross_venue_suspicious "
                    f"WHERE event_id IN ({placeholders})",
                    ev_ids,
                ).fetchall()
                import json as _j
                for r in rs:
                    rd = dict(r)
                    if rd.get("reasons_json"):
                        try:
                            rd["reasons"] = _j.loads(rd["reasons_json"])
                        except Exception:
                            rd["reasons"] = []
                    rd.pop("reasons_json", None)
                    suspicion_by_event[r["event_id"]] = rd
    except Exception:
        pass

    # Decorate events with their suspicion score (if any)
    for e in events:
        s = suspicion_by_event.get(e.get("id"))
        if s:
            e["suspicion"] = s

    return {
        "actor_id": actor_id,
        "score": score,
        "recent_events": events,
        "top_correlations": correlations,
        "enforcement_history": enforcement_history,
    }


# ─── Daily email digest ─────────────────────────────────────────────
# A scheduler task wakes every 60s and checks: is it ≥ DIGEST_HOUR_LOCAL
# in DIGEST_TZ, AND have we not yet sent today? If both, run a pass.
# This keeps timing logic out of cron and means a server restart at any
# time of day still picks up the next due send.
_last_digest_pass: dict = {}
_last_digest_ts: float = 0


async def _digest_monitor():
    global _last_digest_pass, _last_digest_ts
    try:
        from email_digest import run_daily_pass, DIGEST_HOUR_LOCAL, DIGEST_TZ_NAME
        from zoneinfo import ZoneInfo
    except ImportError as e:
        logging.warning("email_digest unavailable: %s", e)
        return

    try:
        tz = ZoneInfo(DIGEST_TZ_NAME)
    except Exception:
        from datetime import timezone as _tz
        tz = _tz.utc

    await asyncio.sleep(120)
    while True:
        from datetime import datetime
        now = datetime.now(tz)
        # Fire only on the dot of the configured hour. The per-user
        # idempotency check inside run_daily_pass prevents double-sends.
        if now.hour == DIGEST_HOUR_LOCAL:
            try:
                result = await asyncio.to_thread(run_daily_pass)
                _last_digest_pass = result
                _last_digest_ts = time.time()
                if result.get("sent", 0) > 0:
                    logging.info(
                        "Digest pass: users=%d sent=%d skipped=%d errored=%d",
                        result.get("users", 0), result.get("sent", 0),
                        result.get("skipped", 0), result.get("errored", 0),
                    )
            except Exception as e:
                logging.error("Digest pass error: %s", e)
        # Sleep until the next minute boundary
        await asyncio.sleep(60)


@app.on_event("startup")
async def _start_digest_monitor():
    task = asyncio.create_task(_digest_monitor())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@app.get("/api/digest/status")
async def digest_status():
    try:
        from email_digest import status_summary
    except ImportError:
        raise HTTPException(503, "email_digest unavailable")
    return {
        "config": await asyncio.to_thread(status_summary),
        "last_pass": {
            "ran_at": int(_last_digest_ts) if _last_digest_ts else None,
            "result": _last_digest_pass or None,
        },
    }


@app.post("/api/digest/send-now")
async def digest_send_now(request: Request):
    """
    Compose and immediately send a digest for the calling user. Useful
    for testing the SMTP setup or grabbing a manual snapshot. Honours
    the per-day idempotency check unless force=true is passed.
    """
    try:
        from email_digest import send_digest
    except ImportError:
        raise HTTPException(503, "email_digest unavailable")
    try:
        body = await request.json()
    except Exception:
        body = {}
    force = bool(body.get("force") if isinstance(body, dict) else False)
    user_id = _watchlist_user_id(request)
    res = await asyncio.to_thread(send_digest, user_id, force=force,
                                  skip_if_empty=False)
    return res


@app.get("/api/digest/preview")
async def digest_preview(request: Request):
    """Build (don't send) the calling user's current digest for in-browser preview."""
    try:
        from email_digest import build_digest_content
    except ImportError:
        raise HTTPException(503, "email_digest unavailable")
    user_id = _watchlist_user_id(request)
    content = await asyncio.to_thread(build_digest_content, user_id)
    return {
        "item_count": content["item_count"],
        "sections": content["sections"],
        "plain": content["plain"],
        # html intentionally omitted from default response — preview UI
        # can hit /api/digest/preview?html=1 if it needs the rich version
    }


# ─── Wallet label management endpoints ──────────────────────────────

@app.get("/api/wallet-labels")
async def wallet_labels_list(
    source: str | None = Query(None, pattern=r"^(manual|polymarket|research|auto)$"),
    limit: int = Query(200, ge=1, le=1000),
):
    """List known wallet labels (e.g. for an admin/labeling UI)."""
    try:
        from wallet_labels import list_labels, stats_summary
    except ImportError:
        raise HTTPException(503, "wallet_labels module unavailable")
    return {
        "labels": list_labels(source=source, limit=limit),
        "summary": stats_summary(),
        "last_bridge": {
            "ran_at": int(_last_bridge_ts) if _last_bridge_ts else None,
            "result": _last_bridge_pass or None,
        },
    }


@app.post("/api/wallet-labels")
async def wallet_labels_set(request: Request):
    """
    Manually attach a display name to a wallet. Body:
      { "address": "0x…", "display_name": "Domer", "twitter": "@domer", "notes": "..." }
    Manual labels are sticky — auto-import won't overwrite them.
    """
    try:
        from wallet_labels import set_label
    except ImportError:
        raise HTTPException(503, "wallet_labels module unavailable")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON")
    addr = (body.get("address") or "").strip()
    name = (body.get("display_name") or "").strip()
    if not addr or not name:
        raise HTTPException(400, "address and display_name are required")
    try:
        wrote = await asyncio.to_thread(
            set_label, addr, name,
            source="manual",
            twitter=(body.get("twitter") or None),
            notes=(body.get("notes") or None),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "written": wrote}


@app.delete("/api/wallet-labels")
async def wallet_labels_delete(
    address: str = Query(..., min_length=10, max_length=42, pattern=r"^0x[a-fA-F0-9]{40}$"),
):
    try:
        from wallet_labels import delete_label
    except ImportError:
        raise HTTPException(503, "wallet_labels module unavailable")
    deleted = await asyncio.to_thread(delete_label, address)
    return {"ok": True, "deleted": deleted}


@app.get("/api/insider-events")
async def insider_events_api(
    venue: str | None = Query(None, pattern=r"^(sec_form4|congress_ptr|13f|polymarket|kalshi)$"),
    limit: int = Query(100, ge=1, le=500),
    since_hours: int | None = Query(None, ge=1, le=24 * 90),
):
    """Recent insider events across all venues (Form 4, eventually congress + 13F)."""
    try:
        from insider_events import recent_events, stats_summary
    except ImportError:
        raise HTTPException(503, "insider_events module unavailable")
    since_ts = int(time.time() - since_hours * 3600) if since_hours else None
    events = recent_events(venue=venue, limit=limit, since_ts=since_ts)

    # Decorate each event with its cross-venue suspicion score (best-effort
    # — the cross_venue_suspicious table may be absent during cold-start).
    if events:
        try:
            import sqlite3 as _sql
            from pathlib import Path as _P
            import json as _j
            ev_ids = [e["id"] for e in events if e.get("id")]
            if ev_ids:
                placeholders = ",".join("?" for _ in ev_ids)
                with _sql.connect(_P(__file__).parent / "insider_events.db") as conn:
                    conn.row_factory = _sql.Row
                    rows = conn.execute(
                        f"SELECT event_id, score, reasons_json "
                        f"FROM cross_venue_suspicious WHERE event_id IN ({placeholders})",
                        ev_ids,
                    ).fetchall()
                susp_by_id = {}
                for r in rows:
                    rd = {"score": r["score"]}
                    if r["reasons_json"]:
                        try:
                            rd["reasons"] = _j.loads(r["reasons_json"])
                        except Exception:
                            rd["reasons"] = []
                    susp_by_id[r["event_id"]] = rd
                for e in events:
                    s = susp_by_id.get(e.get("id"))
                    if s:
                        e["suspicion"] = s
        except Exception:
            pass

    return {
        "events": events,
        "summary": stats_summary(),
        "form4_last_ingest": {
            "ran_at": int(_last_form4_ts) if _last_form4_ts else None,
            "result": _last_form4_ingest or None,
        },
    }


@app.get("/api/insider-events/by-symbol")
async def insider_events_by_symbol(
    symbol: str = Query(..., min_length=1, max_length=12, pattern=r"^[A-Za-z0-9.\-]+$"),
    limit: int = Query(50, ge=1, le=200),
):
    """All insider events for a given ticker symbol."""
    try:
        from insider_events import events_for_symbol
    except ImportError:
        raise HTTPException(503, "insider_events module unavailable")
    return {"symbol": symbol.upper(), "events": events_for_symbol(symbol, limit=limit)}


@app.get("/api/insider-events/by-actor")
async def insider_events_by_actor(
    actor_id: str = Query(..., min_length=1, max_length=80),
    limit: int = Query(100, ge=1, le=500),
):
    """All insider events from a specific actor (CIK, congressperson, wallet)."""
    try:
        from insider_events import events_for_actor
    except ImportError:
        raise HTTPException(503, "insider_events module unavailable")
    return {"actor_id": actor_id, "events": events_for_actor(actor_id, limit=limit)}


@app.get("/api/suspicious-trades")
async def suspicious_trades_api():
    """Return the latest suspicious trade scan results."""
    if not _last_sus_scan:
        return {"suspicious_trades": [], "aggregate_stats": {}, "wallet_investigations": {}, "stale": True}
    stale = (time.time() - _last_sus_scan_time) > SUS_CACHE_TTL if _last_sus_scan_time else False
    result = dict(_last_sus_scan)
    result["stale"] = stale
    result["scan_age_seconds"] = int(time.time() - _last_sus_scan_time) if _last_sus_scan_time else None
    return result


@app.get("/api/retroactive-winners")
async def retroactive_winners_api():
    """Repeat long-shot winners discovered by analyzing resolved markets."""
    retro = _last_sus_scan.get("retroactive") if _last_sus_scan else None
    if not retro:
        return {"profiles": [], "scan_time": None, "markets_scanned": 0}
    return retro


@app.get("/api/bayesian-scores")
async def bayesian_scores_api(min_bets: int = Query(2, ge=1, le=100), limit: int = Query(50, ge=1, le=200)):
    """Top wallets ranked by Bayesian P(edge > baseline)."""
    try:
        from bayesian_wallets import top_wallets_by_edge, stats_summary
    except ImportError:
        raise HTTPException(503, "Bayesian module unavailable")
    return {
        "summary": stats_summary(),
        "wallets": top_wallets_by_edge(limit=limit, min_bets=min_bets),
    }


@app.get("/api/wallet-ml-scores")
async def wallet_ml_scores_api():
    """ML-based wallet anomaly + ranking scores from the latest scan."""
    ml = _last_sus_scan.get("ml") if _last_sus_scan else None
    if not ml:
        return {"available_models": {}, "wallet_count": 0, "combined": [], "isolation_forest": [], "xgboost": []}
    return ml


@app.get("/api/wallet-clusters")
async def wallet_clusters_api():
    """Co-trading sybil/coordination clusters detected from live trades."""
    clusters = _last_sus_scan.get("clusters") if _last_sus_scan else None
    if not clusters:
        return {
            "cluster_count": 0,
            "wallets_in_clusters": 0,
            "edges_total": 0,
            "clusters": [],
            "params": {},
        }
    return clusters


# ─── Copy-trade endpoints ────────────────────────────────────────────

@app.get("/api/profit-leaderboard")
async def profit_leaderboard(
    window: str = Query("all"),
    limit: int = Query(10, ge=1, le=50),
):
    """Top traders by realized PnL (the 'who's actually winning' view)."""
    if window not in ALLOWED_WINDOWS:
        raise HTTPException(400, f"window must be one of {sorted(ALLOWED_WINDOWS)}")

    key = f"profit:{window}:{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient() as client:
        try:
            data = await _fetch_json(
                client, f"{LB_API}/profit", {"window": window, "limit": limit}
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Polymarket profit leaderboard fetch failed: {e}")

    _cache_set(key, data)
    return data


@app.get("/api/trader-quality")
async def trader_quality_api(
    limit: int = Query(50, ge=1, le=200),
    min_bets: int = Query(5, ge=1, le=100),
    min_roi: float | None = Query(None),
):
    """Top wallets by sustainable-edge quality score (copy-trade ranking)."""
    try:
        from trader_quality import top_quality_traders, quality_summary
    except ImportError:
        raise HTTPException(503, "Trader quality module unavailable")

    # Exclude wallets in active sybil clusters from copy-trade rankings
    excluded: set[str] = set()
    clusters = _last_sus_scan.get("clusters") if _last_sus_scan else None
    if clusters:
        for c in clusters.get("clusters", []):
            for w in c.get("wallets", []):
                excluded.add(w.lower())

    return {
        "summary": quality_summary(),
        "excluded_cluster_wallets": len(excluded),
        "traders": top_quality_traders(
            limit=limit,
            min_bets=min_bets,
            exclude_addresses=excluded,
            min_roi=min_roi,
        ),
    }


@app.get("/api/smart-money")
async def smart_money_api():
    """Aggregated open positions of top quality traders (consensus signal)."""
    flow = _last_sus_scan.get("smart_money") if _last_sus_scan else None
    if not flow:
        return {"flows": [], "wallets_scanned": 0, "total_positions": 0, "consensus_markets": 0}
    return flow


@app.get("/api/wallet-metadata")
async def wallet_metadata_api(
    address: str = Query(..., min_length=10, max_length=42, pattern=r"^0x[a-fA-F0-9]{40}$"),
):
    """Polygonscan wallet age + funding source (best-effort, may return null)."""
    try:
        from wallet_metadata import get_wallet_metadata, is_available
    except ImportError:
        return {"available": False, "metadata": None}
    if not is_available():
        return {"available": False, "metadata": None}
    meta = get_wallet_metadata(address)
    return {"available": True, "metadata": meta}


@app.get("/api/wallet-detail")
async def wallet_detail_api(address: str = Query(..., min_length=10, max_length=42, pattern=r"^0x[a-fA-F0-9]{40}$")):
    """Aggregated detail view for a single wallet across all scoring sources."""
    try:
        from bayesian_wallets import score_wallet
    except ImportError:
        score_wallet = None  # type: ignore
    try:
        from trader_quality import score_wallet_quality
    except ImportError:
        score_wallet_quality = None  # type: ignore
    try:
        from wallet_metadata import get_wallet_metadata, is_available as meta_avail
    except ImportError:
        get_wallet_metadata = None  # type: ignore
        meta_avail = lambda: False  # type: ignore

    addr_l = address.lower()
    bayesian = score_wallet(addr_l) if score_wallet else None
    quality = score_wallet_quality(addr_l) if score_wallet_quality else None
    metadata = None
    if get_wallet_metadata and meta_avail():
        try:
            metadata = get_wallet_metadata(addr_l)
        except Exception:
            metadata = None

    ml_entry = None
    retro_profile = None
    flagged_trades = []
    investigation = None
    cluster_membership = None

    if _last_sus_scan:
        for r in _last_sus_scan.get("ml", {}).get("combined", []):
            if r.get("wallet", "").lower() == addr_l:
                ml_entry = r
                break
        retro = _last_sus_scan.get("retroactive", {})
        for p in retro.get("profiles", []):
            if (p.get("wallet") or "").lower() == addr_l:
                retro_profile = p
                break
        for t in _last_sus_scan.get("suspicious_trades", []):
            if (t.get("wallet") or "").lower() == addr_l:
                flagged_trades.append(t)
        investigations = _last_sus_scan.get("wallet_investigations", {})
        for k, v in investigations.items():
            if k.lower() == addr_l:
                investigation = v
                break
        # Cluster membership
        clusters = _last_sus_scan.get("clusters", {})
        for c in clusters.get("clusters", []):
            wallets_lower = {w.lower() for w in c.get("wallets", [])}
            if addr_l in wallets_lower:
                cluster_membership = {
                    "cluster_id": c.get("cluster_id"),
                    "size": c.get("wallet_count"),
                    "score": c.get("score"),
                    "is_recurring": bool(c.get("is_recurring")),
                    "seen_count": c.get("seen_count", 1),
                }
                break

    return {
        "address": addr_l,
        "bayesian": bayesian,
        "quality": quality,
        "metadata": metadata,
        "ml": ml_entry,
        "retroactive_profile": retro_profile,
        "flagged_trades": flagged_trades,
        "investigation": investigation,
        "cluster_membership": cluster_membership,
    }


# ─── Kalshi integration ──────────────────────────────────────────────
#
# Kalshi has no public top-traders leaderboard (KYC platform, no public
# trader identities), so we mirror what is feasible:
#   • Top markets by 24h volume       (public, no auth)
#   • Cross-venue spread vs Polymarket (public, no auth)
#   • The user's own portfolio        (Kalshi API key + RSA private key)
#
# Credentials are encrypted on disk via Fernet (kalshi_creds.py).

def _kalshi_user_id(request: Request) -> str:
    """Map an authenticated request to a credential row.

    Behind the gateway the upstream injects an x-user-id header. In DEV_MODE
    we collapse to a single 'default' user so the dashboard is usable as-is.
    """
    if _DEV_MODE and not _sso_secret:
        return "default"
    uid = request.headers.get("x-user-id")
    if not uid:
        raise HTTPException(400, "Missing x-user-id header")
    return uid


_KALSHI_MARKETS_CACHE: dict = {"data": None, "fetched_at": 0.0}
_KALSHI_MARKETS_TTL = 300  # 5 min — public market data refresh
_KALSHI_ARB_CACHE: dict = {"data": None, "fetched_at": 0.0}
_KALSHI_ARB_TTL = 600  # 10 min — both legs are public


@app.get("/api/kalshi/top-markets")
async def kalshi_top_markets(limit: int = Query(50, ge=1, le=200)):
    """Top Kalshi markets by 24h volume — public data, no auth needed."""
    try:
        from kalshi_client import fetch_top_markets
    except ImportError:
        raise HTTPException(503, "Kalshi client unavailable")

    now = time.time()
    cached = _KALSHI_MARKETS_CACHE["data"]
    if cached and (now - _KALSHI_MARKETS_CACHE["fetched_at"]) < _KALSHI_MARKETS_TTL:
        return {"markets": cached[:limit], "cached": True, "fetched_at": _KALSHI_MARKETS_CACHE["fetched_at"]}

    try:
        markets = await asyncio.to_thread(fetch_top_markets, 200)
    except Exception as e:
        logging.warning("Kalshi top-markets fetch failed: %s", e)
        if cached:
            return {"markets": cached[:limit], "cached": True, "stale": True, "fetched_at": _KALSHI_MARKETS_CACHE["fetched_at"]}
        raise HTTPException(502, f"Kalshi fetch failed: {e}")

    _KALSHI_MARKETS_CACHE["data"] = markets
    _KALSHI_MARKETS_CACHE["fetched_at"] = now
    return {"markets": markets[:limit], "cached": False, "fetched_at": now}


@app.get("/api/kalshi/cross-venue")
async def kalshi_cross_venue(
    kalshi_top_n: int = Query(150, ge=10, le=500),
    poly_top_n: int = Query(250, ge=10, le=500),
):
    """Cross-venue spread opportunities: Kalshi vs Polymarket on the same event."""
    try:
        from kalshi_arbitrage import run_cross_venue_scan
    except ImportError:
        raise HTTPException(503, "Cross-venue scanner unavailable")

    now = time.time()
    cached = _KALSHI_ARB_CACHE["data"]
    if cached and (now - _KALSHI_ARB_CACHE["fetched_at"]) < _KALSHI_ARB_TTL:
        return {**cached, "cached": True, "fetched_at": _KALSHI_ARB_CACHE["fetched_at"]}

    try:
        result = await asyncio.to_thread(run_cross_venue_scan, kalshi_top_n, poly_top_n)
    except Exception as e:
        logging.warning("Kalshi cross-venue scan failed: %s", e)
        if cached:
            return {**cached, "cached": True, "stale": True, "fetched_at": _KALSHI_ARB_CACHE["fetched_at"]}
        raise HTTPException(502, f"Cross-venue scan failed: {e}")

    _KALSHI_ARB_CACHE["data"] = result
    _KALSHI_ARB_CACHE["fetched_at"] = now
    return {**result, "cached": False, "fetched_at": now}


@app.get("/api/kalshi/connection")
async def kalshi_connection_status(request: Request):
    """Whether the user has connected their Kalshi account, plus the key hint."""
    try:
        from kalshi_creds import get_status
    except ImportError:
        return {"connected": False, "available": False}
    return {"available": True, **get_status(_kalshi_user_id(request))}



@app.post("/api/kalshi/connect")
async def kalshi_connect(request: Request):
    """Encrypt and store the caller's Kalshi API key + RSA private key.

    Body: { "api_key": "...", "private_key_pem": "-----BEGIN PRIVATE KEY-----..." }
    """
    try:
        from kalshi_creds import save_creds
        from kalshi_client import KalshiClient
    except ImportError as e:
        raise HTTPException(503, f"Kalshi client unavailable: {e}")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON")

    api_key = (body.get("api_key") or "").strip()
    private_key_pem = (body.get("private_key_pem") or "").strip()
    if not api_key or not private_key_pem:
        raise HTTPException(400, "api_key and private_key_pem are required")
    if "BEGIN" not in private_key_pem or "PRIVATE KEY" not in private_key_pem:
        raise HTTPException(400, "private_key_pem must be a PEM-formatted private key")

    # Verify credentials work before saving
    try:
        client = KalshiClient(api_key, private_key_pem)
        test = await asyncio.to_thread(client.test_connection)
    except Exception as e:
        raise HTTPException(400, f"Failed to load Kalshi client: {e}")
    if not test.get("ok"):
        raise HTTPException(401, f"Kalshi credentials rejected: {test.get('error')}")

    user_id = _kalshi_user_id(request)
    await asyncio.to_thread(save_creds, user_id, api_key, private_key_pem)
    return {"ok": True}


@app.delete("/api/kalshi/connect")
async def kalshi_disconnect(request: Request):
    """Wipe stored Kalshi credentials for the caller."""
    try:
        from kalshi_creds import delete_creds
    except ImportError:
        raise HTTPException(503, "Kalshi credential storage unavailable")
    user_id = _kalshi_user_id(request)
    deleted = await asyncio.to_thread(delete_creds, user_id)
    return {"ok": True, "deleted": deleted}


@app.get("/api/kalshi/portfolio")
async def kalshi_portfolio(request: Request):
    """Return the caller's Kalshi balance + positions + recent fills."""
    try:
        from kalshi_creds import get_creds
        from kalshi_client import fetch_portfolio_summary
    except ImportError as e:
        raise HTTPException(503, f"Kalshi client unavailable: {e}")

    user_id = _kalshi_user_id(request)
    creds = await asyncio.to_thread(get_creds, user_id)
    if not creds:
        return {"connected": False}

    try:
        result = await asyncio.to_thread(
            fetch_portfolio_summary,
            creds["api_key"],
            creds["private_key_pem"],
        )
    except Exception as e:
        logging.warning("Kalshi portfolio fetch failed: %s", e)
        raise HTTPException(502, f"Kalshi portfolio fetch failed: {e}")

    if not result.get("ok"):
        raise HTTPException(502, f"Kalshi portfolio error: {result.get('error')}")
    return {"connected": True, **result}


# ─── Frontend ─────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    if not INDEX_HTML.exists():
        return HTMLResponse("<h1>index.html missing</h1>", status_code=500)
    return HTMLResponse(INDEX_HTML.read_text())


@app.get("/favicon.png")
async def favicon_png():
    if FAVICON_PNG.exists():
        return FileResponse(str(FAVICON_PNG), media_type="image/png")
    return Response(status_code=404)


@app.get("/favicon.ico")
async def favicon_ico():
    if FAVICON_PNG.exists():
        return FileResponse(str(FAVICON_PNG), media_type="image/png")
    return Response(status_code=404)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
