#!/usr/bin/env python3
"""Major Disasters Dashboard - FastAPI backend.

Live disaster tracking + Polymarket edge for disaster prediction markets.

Surfaces (as of v0.x):
  - Active threats: NHC tropical cyclones, NWS severe-weather alerts, EONET
    open events (wildfires/severeStorms/volcanoes/floods), recent USGS
    earthquakes (M5+), GDACS red/orange events globally, Smithsonian GVP
    weekly volcanic activity bulletin, SPC daily storm reports.
  - Year-end projections: Atlantic named/hurricane/major-hurricane counts,
    global M5/M6/M7+ earthquakes (Poisson), NIFC wildfire acres burned
    (calendar-prior-anchored), EONET wildfire counts, US tornadoes (climo),
    FEMA major-disaster (DR) declarations.
  - Drought & impact: USDM categorical percentages, USGS significant-events
    feed (PAGER alert level + felt reports + tsunami flag).
  - Polymarket markets joined with model probabilities + edges.
  - Backtest panel: each year-end count projection replayed against the
    realised actual for the last 5 completed years.

Auth: same gateway-SSO pattern as central-bank-dashboard. Set DEV_MODE=1 to
bypass when running locally.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from analysis import backtest as backtest_mod
from analysis import market_matcher
from ingestion import (
    eonet_events,
    fema_declarations,
    gdacs_alerts,
    nhc_storms,
    nifc_fires,
    nws_alerts,
    polymarket_client,
    smithsonian_volcanoes,
    spc_tornadoes,
    usdm_drought,
    usgs_quakes,
    usgs_significant,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("disasters")

app = FastAPI(title="Major Disasters Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off - all requests will 503")


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


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "disasters-dashboard", "ts": time.time()}


@app.get("/api/health")
async def api_health() -> dict:
    return {"ok": True, "service": "disasters-dashboard", "ts": time.time()}


# ─── Earthquakes ──────────────────────────────────────────────────────────────

@app.get("/api/quakes")
async def api_quakes(min_magnitude: float = 5.0, days: int = 30) -> JSONResponse:
    min_magnitude = max(0.0, min(min_magnitude, 9.0))
    days = max(1, min(days, 365))
    return JSONResponse(usgs_quakes.recent_quakes(min_magnitude=min_magnitude, days=days))


@app.get("/api/quakes/projection")
async def api_quakes_projection(min_magnitude: float = 5.0) -> JSONResponse:
    min_magnitude = max(4.0, min(min_magnitude, 8.0))
    return JSONResponse(usgs_quakes.year_end_projection(min_magnitude=min_magnitude))


@app.get("/api/quakes/significant")
async def api_quakes_significant(window: str = "month") -> JSONResponse:
    if window not in {"week", "month"}:
        window = "month"
    return JSONResponse(usgs_significant.significant_recent(window))


# ─── Tropical cyclones (NHC) ──────────────────────────────────────────────────

@app.get("/api/storms")
async def api_storms() -> JSONResponse:
    return JSONResponse(nhc_storms.active_storms())


@app.get("/api/storms/projection")
async def api_storms_projection() -> JSONResponse:
    return JSONResponse(nhc_storms.atlantic_season_projection())


# ─── NWS alerts ───────────────────────────────────────────────────────────────

@app.get("/api/alerts")
async def api_alerts(severity: str = "Severe") -> JSONResponse:
    return JSONResponse(nws_alerts.active_alerts(severity=severity))


# ─── EONET ────────────────────────────────────────────────────────────────────

@app.get("/api/eonet")
async def api_eonet(category: str = "all") -> JSONResponse:
    return JSONResponse(eonet_events.open_events(category=category))


@app.get("/api/eonet/projection")
async def api_eonet_projection(category: str = "wildfires") -> JSONResponse:
    return JSONResponse(eonet_events.year_end_count_projection(category=category))


# ─── GDACS severity feed ──────────────────────────────────────────────────────

@app.get("/api/gdacs")
async def api_gdacs(min_alert: str = "Orange") -> JSONResponse:
    return JSONResponse(gdacs_alerts.active_events(min_alert=min_alert))


# ─── NIFC wildfires ───────────────────────────────────────────────────────────

@app.get("/api/fires/active")
async def api_fires_active() -> JSONResponse:
    return JSONResponse(nifc_fires.active_incidents())


@app.get("/api/fires/projection")
async def api_fires_projection() -> JSONResponse:
    return JSONResponse(nifc_fires.acres_burned_year_end_projection())


# ─── Tornadoes ────────────────────────────────────────────────────────────────

@app.get("/api/tornadoes")
async def api_tornadoes() -> JSONResponse:
    return JSONResponse(spc_tornadoes.daily_storm_reports())


@app.get("/api/tornadoes/projection")
async def api_tornadoes_projection() -> JSONResponse:
    return JSONResponse(spc_tornadoes.ytd_tornado_projection())


# ─── Volcanoes (Smithsonian GVP) ──────────────────────────────────────────────

@app.get("/api/volcanoes")
async def api_volcanoes() -> JSONResponse:
    return JSONResponse(smithsonian_volcanoes.weekly_active())


# ─── Drought (USDM) ───────────────────────────────────────────────────────────

@app.get("/api/drought")
async def api_drought(aoi: str = "conus") -> JSONResponse:
    if aoi not in {"conus", "total"}:
        aoi = "conus"
    return JSONResponse(usdm_drought.latest_categorical(aoi=aoi))


# ─── FEMA disaster declarations ───────────────────────────────────────────────

@app.get("/api/fema/recent")
async def api_fema_recent(days: int = 30) -> JSONResponse:
    days = max(1, min(days, 365))
    return JSONResponse(fema_declarations.recent_declarations(days=days))


@app.get("/api/fema/projection")
async def api_fema_projection() -> JSONResponse:
    return JSONResponse(fema_declarations.ytd_count_projection())


# ─── Polymarket disaster markets ──────────────────────────────────────────────

def _fetch_all_projections() -> dict:
    """Fetch every projection used by the market matcher in one place."""
    return {
        "storm_proj": nhc_storms.atlantic_season_projection(),
        "quake_projections": {
            5.0: usgs_quakes.year_end_projection(min_magnitude=5.0),
            6.0: usgs_quakes.year_end_projection(min_magnitude=6.0),
            7.0: usgs_quakes.year_end_projection(min_magnitude=7.0),
        },
        "wildfire_count_proj": eonet_events.year_end_count_projection(category="wildfires"),
        "wildfire_acres_proj": nifc_fires.acres_burned_year_end_projection(),
        "tornado_proj": spc_tornadoes.ytd_tornado_projection(),
        "fema_proj": fema_declarations.ytd_count_projection(),
    }


@app.get("/api/markets")
async def api_markets() -> JSONResponse:
    markets = polymarket_client.fetch_disaster_markets()
    projs = _fetch_all_projections()
    enriched = market_matcher.enrich_markets(markets, **projs)
    return JSONResponse({
        "markets": enriched,
        "count": len(enriched),
        "scored_count": sum(1 for m in enriched if m.get("_model_p") is not None),
        "by_model": _count_by_model(enriched),
        "projections": projs,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


def _count_by_model(markets: list[dict]) -> dict:
    out: dict[str, int] = {}
    for m in markets:
        key = m.get("_model_used") or "unscored"
        out[key] = out.get(key, 0) + 1
    return out


# ─── Backtest panel ───────────────────────────────────────────────────────────

@app.get("/api/backtest")
async def api_backtest(n_years: int = 5) -> JSONResponse:
    n_years = max(1, min(n_years, 10))
    return JSONResponse({
        "atlantic_storms": backtest_mod.atlantic_storm_backtest(n_years=n_years),
        "wildfire_acres": backtest_mod.wildfire_acres_backtest(n_years=n_years),
        "method": backtest_mod.methodology(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Single-shot summary ──────────────────────────────────────────────────────

async def _to_thread(fn, *args, **kwargs):
    """Run a sync ingestion call on the default executor so /api/summary can
    fan out to a dozen upstreams concurrently without blocking the event loop."""
    return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args, **kwargs))


@app.get("/api/summary")
async def api_summary() -> JSONResponse:
    """Single payload for the front page. Fans out to every ingestion module
    in parallel via ``asyncio.gather`` so a slow upstream doesn't dominate
    page-load latency."""
    (storms, quakes_recent, eonet, alerts, gdacs, fires_active, fires_proj,
     storm_proj, quake_m5, quake_m6, quake_m7,
     fire_count_proj, tornado_proj, tornadoes_today, fema_proj, drought,
     volcanoes, sig_quakes) = await asyncio.gather(
        _to_thread(nhc_storms.active_storms),
        _to_thread(usgs_quakes.recent_quakes, 5.0, 30),
        _to_thread(eonet_events.open_events, "all"),
        _to_thread(nws_alerts.active_alerts, "Severe"),
        _to_thread(gdacs_alerts.active_events, "Orange"),
        _to_thread(nifc_fires.active_incidents),
        _to_thread(nifc_fires.acres_burned_year_end_projection),
        _to_thread(nhc_storms.atlantic_season_projection),
        _to_thread(usgs_quakes.year_end_projection, 5.0),
        _to_thread(usgs_quakes.year_end_projection, 6.0),
        _to_thread(usgs_quakes.year_end_projection, 7.0),
        _to_thread(eonet_events.year_end_count_projection, "wildfires"),
        _to_thread(spc_tornadoes.ytd_tornado_projection),
        _to_thread(spc_tornadoes.daily_storm_reports),
        _to_thread(fema_declarations.ytd_count_projection),
        _to_thread(usdm_drought.latest_categorical, "conus"),
        _to_thread(smithsonian_volcanoes.weekly_active),
        _to_thread(usgs_significant.significant_recent, "month"),
    )
    return JSONResponse({
        "active": {
            "named_storms": storms.get("storms", []),
            "named_storms_count": len(storms.get("storms", [])),
            "alerts_count": alerts.get("count", 0),
            "alerts_top": alerts.get("alerts", [])[:5],
            "alerts_by_event": alerts.get("by_event", {}),
            "wildfires_count_eonet": eonet.get("by_category", {}).get("wildfires", 0),
            "severe_storms_count": eonet.get("by_category", {}).get("severeStorms", 0),
            "volcanoes_count_eonet": eonet.get("by_category", {}).get("volcanoes", 0),
            "us_active_fires": fires_active.get("count", 0),
            "us_active_acres": fires_active.get("active_acres_total", 0),
            "us_active_fires_top": fires_active.get("incidents", [])[:6],
            "tornado_reports_today": tornadoes_today.get("tornado_count", 0),
            "hail_reports_today": tornadoes_today.get("hail_count", 0),
            "wind_reports_today": tornadoes_today.get("wind_count", 0),
            "gvp_volcanoes_count": volcanoes.get("count", 0),
            "gvp_volcanoes_top": volcanoes.get("volcanoes", [])[:6],
        },
        "gdacs": {
            "count": gdacs.get("count", 0),
            "by_alert": gdacs.get("by_alert_level", {}),
            "events_top": gdacs.get("events", [])[:8],
        },
        "drought": drought,
        "recent_quakes": {
            "count_30d": quakes_recent.get("count", 0),
            "biggest": quakes_recent.get("biggest"),
            "m6_plus_30d": sum(1 for q in quakes_recent.get("quakes", []) if (q.get("mag") or 0) >= 6.0),
            "m7_plus_30d": sum(1 for q in quakes_recent.get("quakes", []) if (q.get("mag") or 0) >= 7.0),
            "significant_30d_count": sig_quakes.get("count", 0),
            "significant_alerts": sig_quakes.get("by_alert", {}),
        },
        "projections": {
            "atlantic_storms": storm_proj,
            "quakes_m5": quake_m5,
            "quakes_m6": quake_m6,
            "quakes_m7": quake_m7,
            "wildfires_count": fire_count_proj,
            "wildfires_acres": fires_proj,
            "tornadoes": tornado_proj,
            "fema_dr": fema_proj,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "7053"))
    log.info("Starting disasters dashboard on :%d", port)
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "0.0.0.0"), port=port)
