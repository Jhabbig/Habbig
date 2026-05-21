#!/usr/bin/env python3
"""Voter Pulse Dashboard — FastAPI backend.

Surface:
  GET /            → index.html
  GET /api/summary → mood index + every life indicator + sentiment markets
  GET /api/life    → just the FRED indicators
  GET /api/markets → just the Polymarket sentiment markets
  GET /api/mood    → just the composite mood score
  GET /healthz

Auth: same gateway-SSO pattern as world-state-dashboard / centralbank.
Set DEV_MODE=1 to bypass when running locally.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from analysis import clark_fisher as world_analysis
from analysis import elections as election_analysis
from analysis import eras as era_analysis
from analysis import mood_index
from analysis import state_mood as state_mood_analysis
from ingestion import fred_client, polls_client, polymarket_client, states_client, worldbank_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Voter Pulse Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"
METHODOLOGY_PATH = Path(__file__).parent / "methodology.html"

# Series we surface in the "by administration" comparison table.
ERA_SERIES = ["CPIAUCSL", "UNRATE", "UMCSENT", "MORTGAGE30US", "GASREGW"]

# Backtest is expensive — monthly sweep from 1978 over many series.
# Cache it for 12h alongside the FRED data.
_BACKTEST_CACHE: dict = {"data": None, "fred_fetched_at": 0.0}

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — all requests will 503")


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    if request.url.path != "/healthz":
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
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/methodology", response_class=HTMLResponse)
async def methodology() -> HTMLResponse:
    return HTMLResponse(METHODOLOGY_PATH.read_text(encoding="utf-8"))


@app.get("/api/life")
async def api_life(force: bool = False) -> JSONResponse:
    return JSONResponse(fred_client.get_cached(force=force))


@app.get("/api/markets")
async def api_markets(force: bool = False) -> JSONResponse:
    return JSONResponse(polymarket_client.get_cached(force=force))


@app.get("/api/polls")
async def api_polls(force: bool = False) -> JSONResponse:
    return JSONResponse(polls_client.get_cached(force=force))


@app.get("/api/eras")
async def api_eras(force: bool = False) -> JSONResponse:
    life = fred_client.get_cached(force=force)
    return JSONResponse(era_analysis.compose(life["series"], ERA_SERIES))


def _backtest_payload(force: bool = False) -> dict:
    life = fred_client.get_cached(force=force)
    fetched_at = life.get("fetched_at") or 0.0
    cached = _BACKTEST_CACHE.get("data")
    if cached is not None and _BACKTEST_CACHE.get("fred_fetched_at") == fetched_at and not force:
        return {**cached, "fred_fetched_at": fetched_at, "cached": True}
    payload = election_analysis.run(life["series"])
    _BACKTEST_CACHE["data"] = payload
    _BACKTEST_CACHE["fred_fetched_at"] = fetched_at
    return {**payload, "fred_fetched_at": fetched_at, "cached": False}


@app.get("/api/backtest")
async def api_backtest(force: bool = False) -> JSONResponse:
    return JSONResponse(_backtest_payload(force=force))


@app.get("/api/states")
async def api_states(force: bool = False) -> JSONResponse:
    raw = states_client.get_cached(force=force)
    return JSONResponse({**state_mood_analysis.compose(raw), "fetched_at": raw.get("fetched_at")})


@app.get("/api/world")
async def api_world(force: bool = False) -> JSONResponse:
    raw = worldbank_client.get_cached(force=force)
    return JSONResponse({
        **world_analysis.summarise(raw["countries"]),
        "fetched_at": raw.get("fetched_at"),
    })


@app.get("/api/mood")
async def api_mood(force: bool = False) -> JSONResponse:
    life = fred_client.get_cached(force=force)
    composed = mood_index.compose(life["series"])
    composed["label"] = mood_index.label_for(composed["overall"])
    return JSONResponse(composed)


@app.get("/api/summary")
async def api_summary(force: bool = False) -> JSONResponse:
    life = fred_client.get_cached(force=force)
    markets = polymarket_client.get_cached(force=force)
    polls = polls_client.get_cached(force=force)
    composed = mood_index.compose(life["series"])
    composed["label"] = mood_index.label_for(composed["overall"])
    eras = era_analysis.compose(life["series"], ERA_SERIES)
    backtest = _backtest_payload(force=force)
    raw_states = states_client.get_cached(force=force)
    states = {**state_mood_analysis.compose(raw_states), "fetched_at": raw_states.get("fetched_at")}
    raw_world = worldbank_client.get_cached(force=force)
    world = {**world_analysis.summarise(raw_world["countries"]), "fetched_at": raw_world.get("fetched_at")}
    return JSONResponse({
        "mood": composed,
        "life": life,
        "markets": markets,
        "polls": polls,
        "eras": eras,
        "backtest": backtest,
        "states": states,
        "world": world,
    })


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "7062")),
    )
