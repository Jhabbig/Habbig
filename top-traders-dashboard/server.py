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
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

# ─── Config ───────────────────────────────────────────────────────────
PORT = 8052
LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
ALLOWED_WINDOWS = {"all", "1d", "7d", "30d"}
CACHE_TTL_SECONDS = 20  # shorter than the 30s frontend poll to avoid serving stale data
HTTP_TIMEOUT = 15.0

HERE = Path(__file__).parent
INDEX_HTML = HERE / "index.html"

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

# Small in-memory cache: { key -> (expires_at, payload) }
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and entry[0] > time.time():
        return entry[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    if len(_cache) > 100:
        _cache.clear()
    _cache[key] = (time.time() + CACHE_TTL_SECONDS, value)


async def _fetch_json(client: httpx.AsyncClient, url: str, params: dict) -> Any:
    r = await client.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


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
    user: str = Query(..., min_length=10),
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
):
    """
    Convenience endpoint: returns the top 3 traders and each trader's recent
    trades in a single call, so the frontend only makes one fetch.
    """
    if window not in ALLOWED_WINDOWS:
        raise HTTPException(400, f"window must be one of {sorted(ALLOWED_WINDOWS)}")

    key = f"top3:{window}:{trades_per_trader}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient() as client:
        try:
            lb = await _fetch_json(
                client, f"{LB_API}/volume", {"window": window, "limit": 3}
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Polymarket leaderboard fetch failed: {e}")

        traders = []
        for rank, entry in enumerate(lb, start=1):
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
            traders.append(
                {
                    "rank": rank,
                    "proxyWallet": wallet,
                    "name": entry.get("name") or entry.get("pseudonym") or wallet[:10],
                    "pseudonym": entry.get("pseudonym"),
                    "volume": entry.get("amount", 0),
                    "profileImage": entry.get("profileImageOptimized")
                    or entry.get("profileImage")
                    or "",
                    "bio": entry.get("bio", ""),
                    "trades": trades,
                }
            )

    payload = {
        "window": window,
        "fetched_at": int(time.time()),
        "traders": traders,
    }
    _cache_set(key, payload)
    return payload


# ─── Suspicious Trades Scanner ───────────────────────────────────────
_last_sus_scan: dict = {}
_last_sus_scan_time: float = 0
SUS_CACHE_TTL = 1800  # serve cached data for 30 min


async def _suspicious_trade_monitor():
    """Periodically scan for suspicious trades in the background."""
    global _last_sus_scan, _last_sus_scan_time
    from suspicious_trades import run_scanner

    await asyncio.sleep(10)  # let server start

    while True:
        try:
            result = await asyncio.to_thread(run_scanner)
            if result and result.get("suspicious_trades"):
                _last_sus_scan = result
                _last_sus_scan_time = time.time()
                logging.info(
                    "Suspicious scan complete: %d flagged trades",
                    len(result["suspicious_trades"]),
                )
        except Exception as e:
            logging.error("Suspicious scanner error: %s", e)

        await asyncio.sleep(1800)  # re-scan every 30 min


@app.on_event("startup")
async def _start_scanner():
    asyncio.create_task(_suspicious_trade_monitor())


@app.get("/api/suspicious-trades")
async def suspicious_trades_api():
    """Return the latest suspicious trade scan results."""
    if not _last_sus_scan:
        return {"suspicious_trades": [], "aggregate_stats": {}, "wallet_investigations": {}}
    return _last_sus_scan


# ─── Frontend ─────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    if not INDEX_HTML.exists():
        return HTMLResponse("<h1>index.html missing</h1>", status_code=500)
    return HTMLResponse(INDEX_HTML.read_text())


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
