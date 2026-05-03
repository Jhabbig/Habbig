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
