#!/usr/bin/env python3
"""Whale Tracker Dashboard — TradFi insider, activist, and M&A signals.

Ingests SEC EDGAR filings (Form 4, SC 13D/G, 8-K), persists them to
SQLite, and exposes ranked feeds + a per-ticker synthesis view. The
heuristic stack (insider clustering, activist filings, 8-K M&A scoring)
is designed to compose with paid options-flow / dark-pool feeds in
phase 2 — none of those are required for the dashboard to work today.

Port: 8053. Behind the gateway at whales.narve.ai in production.
Auth: gateway SSO secret in `x-gateway-secret`, or DEV_MODE=1 to bypass.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import db
import ingest
import signals as signals_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("whale")

PORT = int(os.environ.get("PORT", "8053"))
HERE = Path(__file__).parent
INDEX_HTML = HERE / "index.html"
FAVICON_PNG = HERE / "favicon.png"

app = FastAPI(title="Whale Tracker Dashboard")

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"

if not _sso_secret:
    if _DEV_MODE:
        log.warning("GATEWAY_SSO_SECRET not set — whale tracker running in DEV_MODE (no auth)")
    else:
        log.warning("GATEWAY_SSO_SECRET not set and DEV_MODE not enabled — rejecting all requests")


@app.middleware("http")
async def gateway_auth_middleware(request: Request, call_next):
    """Verify gateway SSO secret on all requests; reject if misconfigured.

    /healthz is exempt so container healthchecks (which can't carry the
    gateway header) succeed. It exposes only table counts + last-ingest
    timestamps, no PII.
    """
    if request.url.path == "/healthz":
        response = await call_next(request)
        return response
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
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'"
    )
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# ─── tiny LRU response cache ──────────────────────────────────────────
# Signal queries are read-only over a slow-changing dataset; a 30s cache
# keeps the API snappy even if a watchlist client polls every few sec.

_CACHE_MAX = 64
_CACHE_TTL = 30.0
_cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()


def _cached(key: str, builder):
    entry = _cache.get(key)
    now = time.time()
    if entry and entry[0] > now:
        _cache.move_to_end(key)
        return entry[1]
    val = builder()
    _cache[key] = (now + _CACHE_TTL, val)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return val


# ─── lifecycle ────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup():
    db.init_db()
    if os.environ.get("DISABLE_INGEST", "").strip() == "1":
        log.warning("ingest disabled via DISABLE_INGEST=1")
        return
    asyncio.create_task(ingest.loop_forever())


# ─── routes ───────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"ok": True, "counts": db.counts(), "ingest": db.get_ingest_state()}


@app.get("/api/insider-clusters")
async def api_insider_clusters(
    days: int = Query(30, ge=1, le=180),
    min_buyers: int = Query(3, ge=2, le=20),
):
    key = f"clusters:{days}:{min_buyers}"
    return _cached(key, lambda: signals_mod.insider_clusters(window_days=days, min_buyers=min_buyers))


@app.get("/api/insider-recent")
async def api_insider_recent(
    days: int = Query(7, ge=1, le=90),
    min_value: float = Query(100_000, ge=0),
):
    key = f"insider_recent:{days}:{min_value}"
    return _cached(key, lambda: signals_mod.recent_insider_buys(window_days=days, min_value_usd=min_value))


@app.get("/api/activist-stakes")
async def api_activist_stakes(days: int = Query(14, ge=1, le=180)):
    return _cached(f"activist:{days}", lambda: signals_mod.recent_activist_stakes(window_days=days))


@app.get("/api/ma-feed")
async def api_ma_feed(
    days: int = Query(7, ge=1, le=90),
    min_score: float = Query(2.0, ge=0),
):
    return _cached(f"ma:{days}:{min_score}", lambda: signals_mod.recent_ma_events(window_days=days, min_score=min_score))


@app.get("/api/synthesis")
async def api_synthesis(
    ticker: str = Query(..., min_length=1, max_length=10),
    days: int = Query(90, ge=1, le=365),
):
    t = ticker.upper().strip()
    return _cached(f"syn:{t}:{days}", lambda: signals_mod.ticker_synthesis(t, window_days=days))


@app.get("/api/whale-leaderboard")
async def api_whale_leaderboard(days: int = Query(90, ge=1, le=365)):
    return _cached(f"whales:{days}", lambda: signals_mod.whale_leaderboard(window_days=days))


@app.post("/api/admin/ingest-now")
async def api_admin_ingest_now():
    """Trigger a manual ingest pass; primarily for local dev / smoke tests."""
    if not _DEV_MODE:
        return JSONResponse({"error": "DEV_MODE only"}, status_code=403)
    res = await ingest.run_once()
    _cache.clear()
    return {"inserted": res, "counts": db.counts()}


# ─── static ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    if INDEX_HTML.exists():
        return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>whale tracker</h1><p>index.html missing</p>", status_code=500)


@app.get("/favicon.png")
async def favicon_png():
    if FAVICON_PNG.exists():
        return FileResponse(FAVICON_PNG)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/favicon.ico")
async def favicon_ico():
    if FAVICON_PNG.exists():
        return FileResponse(FAVICON_PNG, media_type="image/png")
    return JSONResponse({"error": "not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=PORT, log_level="info")
