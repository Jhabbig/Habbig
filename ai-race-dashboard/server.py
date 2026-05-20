#!/usr/bin/env python3
"""AI Race Dashboard — FastAPI backend.

Serves:
  - GET /                    → index.html
  - GET /api/labs            → curated lab snapshots
  - GET /api/models          → frontier model leaderboard (curated + live)
  - GET /api/benchmarks      → benchmark definitions
  - GET /api/timeline        → release timeline
  - GET /api/frontier        → best score per benchmark over time (line series)
  - GET /api/markets         → live Polymarket AI markets (cached)
  - GET /api/sources         → ingestion source status (last fetch, errors)
  - POST /api/refresh        → force refresh all ingestion sources
  - GET /api/health          → liveness probe

A background thread refreshes ingestion sources hourly. Each cell in the
leaderboard is tagged with provenance (`curated` or `live:<source>`) and a
freshness flag.

Runs behind the gateway (HMAC SSO header `x-gateway-secret`). DEV_MODE=1
disables auth for local development.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import data as ai_data
import live_data
import markets as ai_markets
from ingestion import refresh_all

app = FastAPI(title="AI Race Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret and not _DEV_MODE:
    logging.warning(
        "GATEWAY_SSO_SECRET not set and DEV_MODE not enabled — "
        "ai-race dashboard will reject all requests."
    )


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
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
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


def _json(payload) -> JSONResponse:
    return JSONResponse(payload)


# ── Polymarket AI markets (live, cached) ─────────────────────────────────────
_cache_lock = threading.Lock()
POLY_CACHE = {"data": [], "fetched_at": 0.0}
POLY_TTL = 60  # seconds


def _is_ai_market(market: dict) -> bool:
    haystack = " ".join([
        (market.get("question") or "").lower(),
        (market.get("category") or "").lower(),
        (market.get("slug") or "").lower(),
    ])
    return any(kw in haystack for kw in ai_data.AI_MARKET_KEYWORDS)


def fetch_ai_markets() -> list[dict]:
    now = time.time()
    with _cache_lock:
        if POLY_CACHE["data"] and (now - POLY_CACHE["fetched_at"]) < POLY_TTL:
            return POLY_CACHE["data"]

    out: list[dict] = []
    try:
        url = (
            "https://gamma-api.polymarket.com/markets"
            "?closed=false&active=true&limit=200&order=volume24hr&ascending=false"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (AIRaceDashboard/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
        markets = json.loads(raw)
        if not isinstance(markets, list):
            markets = []

        for m in markets:
            if not _is_ai_market(m):
                continue
            try:
                outcomes_raw = m.get("outcomes") or "[]"
                prices_raw = m.get("outcomePrices") or "[]"
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                if not outcomes or not prices:
                    continue

                float_prices = []
                for p in prices:
                    try:
                        float_prices.append(float(p))
                    except (TypeError, ValueError):
                        float_prices.append(0.0)

                top_idx = float_prices.index(max(float_prices)) if float_prices else 0
                top_price = float_prices[top_idx] if float_prices else 0.0
                top_outcome = outcomes[top_idx] if 0 <= top_idx < len(outcomes) else "Yes"

                vol_24h = m.get("volume24hr") or 0
                try:
                    vol_24h = float(vol_24h)
                except (TypeError, ValueError):
                    vol_24h = 0.0

                slug = m.get("slug") or ""
                out.append({
                    "id": m.get("id"),
                    "question": m.get("question") or "",
                    "slug": slug,
                    "url": f"https://polymarket.com/event/{slug}" if slug else "",
                    "end_date": m.get("endDate") or "",
                    "top_outcome": top_outcome,
                    "top_price": top_price,
                    "outcomes": outcomes,
                    "prices": float_prices,
                    "volume_24h": vol_24h,
                })
            except Exception as e:
                logging.warning("polymarket parse error: %s", e)
                continue
    except Exception as e:
        logging.warning("polymarket fetch failed: %s", e)

    out.sort(key=lambda r: r["volume_24h"], reverse=True)
    out = out[:30]
    with _cache_lock:
        POLY_CACHE["data"] = out
        POLY_CACHE["fetched_at"] = now
    return out


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    if not HTML_PATH.exists():
        return HTMLResponse("<h1>index.html missing</h1>", status_code=500)
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health():
    return _json({"ok": True, "ts": datetime.now(timezone.utc).isoformat()})


@app.get("/api/labs")
async def get_labs():
    return _json({"labs": ai_data.LABS, "as_of": ai_data.DATASET_AS_OF})


@app.get("/api/benchmarks")
async def get_benchmarks():
    return _json({"benchmarks": ai_data.BENCHMARKS, "as_of": ai_data.DATASET_AS_OF})


@app.get("/api/models")
async def get_models():
    return _json(live_data.merged_models())


@app.get("/api/sources")
async def get_sources():
    return _json({"sources": live_data.sources_status()})


@app.post("/api/refresh")
async def post_refresh():
    import asyncio
    results = await asyncio.to_thread(refresh_all, True)
    return _json({
        "refreshed": [
            {
                "source": r.get("source"),
                "ok": r.get("ok"),
                "entries": len(r.get("entries", [])),
                "error": r.get("error"),
            }
            for r in results
        ],
    })


@app.get("/api/timeline")
async def get_timeline():
    rows = []
    for ev in ai_data.TIMELINE:
        lab = ai_data.lab_by_key(ev["lab_key"]) or {}
        rows.append({
            **ev,
            "lab_name": lab.get("name", ev["lab_key"]),
            "lab_color": lab.get("color", "#888"),
        })
    return _json({"events": rows, "as_of": ai_data.DATASET_AS_OF})


@app.get("/api/frontier")
async def get_frontier():
    """Running max-score series per benchmark, computed off merged scores."""
    return _json(live_data.merged_frontier())


@app.get("/api/markets")
async def get_markets():
    """Backwards-compat keyword filter — top-volume AI markets on Polymarket."""
    import asyncio
    markets = await asyncio.to_thread(fetch_ai_markets)
    return _json({"markets": markets, "count": len(markets)})


@app.get("/api/markets/featured")
async def get_markets_featured():
    """Curated AI events (Polymarket + Kalshi) with full multi-outcome trees."""
    import asyncio
    payload = await asyncio.to_thread(ai_markets.get_featured)
    return _json(payload)


@app.get("/api/markets/moves")
async def get_markets_moves(min_change: float = 0.05, limit: int = 12):
    """Top 24h price movers among AI-tagged Polymarket questions."""
    import asyncio
    payload = await asyncio.to_thread(ai_markets.get_movers, min_change, limit)
    return _json(payload)


# ── Background ingestion refresher ───────────────────────────────────────────
_REFRESH_INTERVAL_S = 60 * 60  # 1 hour


def _refresh_loop():
    # Initial fetch shortly after startup so first request is already populated.
    time.sleep(3)
    while True:
        try:
            results = refresh_all(force=True)
            ok = sum(1 for r in results if r.get("ok"))
            logging.info("ingestion refresh: %d/%d ok", ok, len(results))
        except Exception as e:  # noqa: BLE001
            logging.warning("ingestion refresh loop error: %s", e)
        time.sleep(_REFRESH_INTERVAL_S)


@app.on_event("startup")
def _start_refresher() -> None:
    if os.environ.get("DISABLE_INGESTION") == "1":
        logging.info("DISABLE_INGESTION=1 — skipping background refresher")
        return
    t = threading.Thread(target=_refresh_loop, name="ingestion-refresher", daemon=True)
    t.start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7070)
