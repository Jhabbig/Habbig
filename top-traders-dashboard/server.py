#!/usr/bin/env python3
"""
Top Traders Dashboard — tracks the top 3 traders on Polymarket and
streams their recent trades.

Data sources (all public, unauthenticated):
  - https://lb-api.polymarket.com/volume?window=<all|1d|7d|30d>&limit=N
      Returns the leaderboard ranked by volume traded in that window.
  - https://data-api.polymarket.com/trades?user=<wallet>&limit=N
      Returns the most recent trades for a given proxy wallet.

Run: python3 server.py   (listens on :8052)
"""

import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

# ─── Config ───────────────────────────────────────────────────────────
PORT = 8052
LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
ALLOWED_WINDOWS = {"all", "1d", "7d", "30d"}
CACHE_TTL_SECONDS = 30
HTTP_TIMEOUT = 15.0

HERE = Path(__file__).parent
INDEX_HTML = HERE / "index.html"

app = FastAPI(title="Polymarket Top Traders Dashboard")

# Small in-memory cache: { key -> (expires_at, payload) }
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and entry[0] > time.time():
        return entry[1]
    return None


def _cache_set(key: str, value: Any) -> None:
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

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
