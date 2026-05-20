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
import news as ai_news
import snapshot as ai_snapshot
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


@app.get("/api/compute")
async def get_compute():
    rows = []
    for c in ai_data.COMPUTE:
        lab = ai_data.lab_by_key(c["lab_key"]) or {}
        rows.append({
            **c,
            "lab_name": lab.get("name", c["lab_key"]),
            "lab_color": lab.get("color", "#888"),
            "country": lab.get("country", ""),
        })
    return _json({"compute": rows, "as_of": ai_data.DATASET_AS_OF})


@app.get("/api/export-controls")
async def get_export_controls():
    return _json({"events": ai_data.EXPORT_CONTROLS, "as_of": ai_data.DATASET_AS_OF})


@app.get("/api/capex")
async def get_capex():
    return _json({
        "quarterly": ai_data.CAPEX_QUARTERLY,
        "tickers": ai_data.CAPEX_TICKERS,
        "as_of": ai_data.DATASET_AS_OF,
    })


def _org_meta(key: str) -> dict:
    lab = ai_data.lab_by_key(key)
    if lab:
        return {"name": lab["name"], "color": lab["color"]}
    return ai_data.TALENT_ORG_LABELS.get(key, {"name": key, "color": "#6b7280"})


@app.get("/api/talent")
async def get_talent():
    moves = []
    for m in ai_data.TALENT_MOVES:
        f = _org_meta(m["from"])
        t = _org_meta(m["to"])
        moves.append({
            **m,
            "from_name": f["name"], "from_color": f["color"],
            "to_name": t["name"],   "to_color": t["color"],
        })
    headcount = []
    for h in ai_data.HEADCOUNT:
        lab = ai_data.lab_by_key(h["lab_key"]) or {}
        headcount.append({
            **h,
            "lab_name": lab.get("name", h["lab_key"]),
            "lab_color": lab.get("color", "#888"),
        })
    return _json({"moves": moves, "headcount": headcount, "as_of": ai_data.DATASET_AS_OF})


@app.get("/api/news")
async def get_news_endpoint():
    import asyncio
    payload = await asyncio.to_thread(ai_news.get_news, False)
    return _json(payload)


@app.get("/api/funding")
async def get_funding():
    rows = []
    for r in ai_data.FUNDING_ROUNDS:
        lab = ai_data.lab_by_key(r["lab_key"]) or {}
        rows.append({
            **r,
            "lab_name": lab.get("name", r["lab_key"]),
            "lab_color": lab.get("color", "#888"),
        })
    # Cumulative raised per lab, in chronological order.
    by_lab: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: x["date"]):
        k = r["lab_key"]
        bucket = by_lab.setdefault(k, {
            "lab_key": k, "lab_name": r["lab_name"], "lab_color": r["lab_color"],
            "rounds": 0, "raised_usd_b": 0.0, "latest_post_usd_b": None,
            "latest_round_date": None,
        })
        bucket["rounds"] += 1
        bucket["raised_usd_b"] += r.get("amount_usd_b", 0) or 0
        if r.get("post_usd_b") is not None:
            bucket["latest_post_usd_b"] = r["post_usd_b"]
        bucket["latest_round_date"] = r["date"]
    totals = sorted(by_lab.values(), key=lambda b: b["raised_usd_b"], reverse=True)
    return _json({"rounds": rows, "totals": totals, "as_of": ai_data.DATASET_AS_OF})


@app.get("/api/stocks")
async def get_stocks():
    return _json({
        "stocks": ai_data.AI_STOCKS,
        "as_of": ai_data.AI_STOCKS_AS_OF,
    })


@app.get("/api/snapshots")
async def get_snapshots():
    return _json({"snapshots": ai_snapshot.list_snapshots()})


@app.get("/api/recent-changes")
async def get_recent_changes(days: int = 7, top: int = 25):
    snaps = ai_snapshot.list_snapshots()
    if not snaps:
        return _json({"changes": [], "since": None, "until": None,
                      "note": "no snapshots yet"})
    until = snaps[-1]
    # Pick the snapshot whose date is at least `days` before `until`.
    from datetime import date, timedelta
    try:
        target = (date.fromisoformat(until) - timedelta(days=days)).isoformat()
    except ValueError:
        target = snaps[0]
    changes = ai_snapshot.compute_deltas(since=target, until=until, top_n=top)
    return _json({"changes": changes, "since": target, "until": until})


@app.get("/api/alerts")
async def get_alerts():
    return _json({"alerts": ai_snapshot.alerts()})


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


_SNAPSHOT_INTERVAL_S = 60 * 60 * 6  # check every 6h; take_snapshot is idempotent per day


def _snapshot_loop():
    time.sleep(8)
    while True:
        try:
            ai_snapshot.take_snapshot()
        except Exception as e:  # noqa: BLE001
            logging.warning("snapshot loop error: %s", e)
        time.sleep(_SNAPSHOT_INTERVAL_S)


@app.on_event("startup")
def _start_refresher() -> None:
    if os.environ.get("DISABLE_INGESTION") != "1":
        threading.Thread(target=_refresh_loop, name="ingestion-refresher", daemon=True).start()
    else:
        logging.info("DISABLE_INGESTION=1 — skipping background refresher")
    if os.environ.get("DISABLE_SNAPSHOTS") != "1":
        threading.Thread(target=_snapshot_loop, name="snapshot-writer", daemon=True).start()
    else:
        logging.info("DISABLE_SNAPSHOTS=1 — skipping snapshot writer")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7070)
