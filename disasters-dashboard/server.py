#!/usr/bin/env python3
"""Major Disasters Dashboard - FastAPI backend.

Live disaster tracking + Polymarket edge for disaster prediction markets.

Surfaces:
  - Active threats: NHC tropical cyclones, NWS severe-weather alerts, EONET
    open events (wildfires, severe storms, volcanoes, floods), recent USGS
    earthquakes (M5+/M6+).
  - Year-end record-pace projections: Atlantic named storms, US tornadoes,
    global M5+ earthquakes, NIFC wildfire acres.
  - Polymarket disaster markets joined with model probabilities (edge column).

Auth: same gateway-SSO pattern as central-bank-dashboard. Set DEV_MODE=1 to
bypass when running locally.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from analysis import market_matcher
from ingestion import (
    eonet_events,
    nhc_storms,
    nws_alerts,
    polymarket_client,
    usgs_quakes,
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


@app.get("/api/quakes")
async def api_quakes(min_magnitude: float = 5.0, days: int = 30) -> JSONResponse:
    min_magnitude = max(0.0, min(min_magnitude, 9.0))
    days = max(1, min(days, 365))
    return JSONResponse(usgs_quakes.recent_quakes(min_magnitude=min_magnitude, days=days))


@app.get("/api/quakes/projection")
async def api_quakes_projection(min_magnitude: float = 5.0) -> JSONResponse:
    min_magnitude = max(4.0, min(min_magnitude, 8.0))
    return JSONResponse(usgs_quakes.year_end_projection(min_magnitude=min_magnitude))


@app.get("/api/storms")
async def api_storms() -> JSONResponse:
    return JSONResponse(nhc_storms.active_storms())


@app.get("/api/storms/projection")
async def api_storms_projection() -> JSONResponse:
    return JSONResponse(nhc_storms.atlantic_season_projection())


@app.get("/api/alerts")
async def api_alerts(severity: str = "Severe") -> JSONResponse:
    return JSONResponse(nws_alerts.active_alerts(severity=severity))


@app.get("/api/eonet")
async def api_eonet(category: str = "all") -> JSONResponse:
    return JSONResponse(eonet_events.open_events(category=category))


@app.get("/api/eonet/projection")
async def api_eonet_projection(category: str = "wildfires") -> JSONResponse:
    return JSONResponse(eonet_events.year_end_count_projection(category=category))


@app.get("/api/markets")
async def api_markets() -> JSONResponse:
    markets = polymarket_client.fetch_disaster_markets()
    storm_proj = nhc_storms.atlantic_season_projection()
    quake_m5 = usgs_quakes.year_end_projection(min_magnitude=5.0)
    quake_m6 = usgs_quakes.year_end_projection(min_magnitude=6.0)
    quake_m7 = usgs_quakes.year_end_projection(min_magnitude=7.0)
    fire_proj = eonet_events.year_end_count_projection(category="wildfires")
    enriched = market_matcher.enrich_markets(
        markets,
        storm_proj=storm_proj,
        quake_projections={5.0: quake_m5, 6.0: quake_m6, 7.0: quake_m7},
        wildfire_proj=fire_proj,
    )
    return JSONResponse({
        "markets": enriched,
        "count": len(enriched),
        "storm_projection": storm_proj,
        "quake_projections": {
            "m5": quake_m5,
            "m6": quake_m6,
            "m7": quake_m7,
        },
        "wildfire_projection": fire_proj,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/summary")
async def api_summary() -> JSONResponse:
    """Single endpoint giving the front page everything it needs in one shot."""
    storms = nhc_storms.active_storms()
    quakes_recent = usgs_quakes.recent_quakes(min_magnitude=5.0, days=30)
    eonet = eonet_events.open_events(category="all")
    alerts = nws_alerts.active_alerts(severity="Severe")
    storm_proj = nhc_storms.atlantic_season_projection()
    quake_m5 = usgs_quakes.year_end_projection(min_magnitude=5.0)
    quake_m6 = usgs_quakes.year_end_projection(min_magnitude=6.0)
    quake_m7 = usgs_quakes.year_end_projection(min_magnitude=7.0)
    fire_proj = eonet_events.year_end_count_projection(category="wildfires")
    return JSONResponse({
        "active": {
            "named_storms": storms.get("storms", []),
            "named_storms_count": len(storms.get("storms", [])),
            "alerts_count": alerts.get("count", 0),
            "alerts_top": alerts.get("alerts", [])[:5],
            "wildfires_count": eonet.get("by_category", {}).get("wildfires", 0),
            "severe_storms_count": eonet.get("by_category", {}).get("severeStorms", 0),
            "volcanoes_count": eonet.get("by_category", {}).get("volcanoes", 0),
        },
        "recent_quakes": {
            "count_30d": quakes_recent.get("count", 0),
            "biggest": quakes_recent.get("biggest"),
            "m6_plus_30d": sum(1 for q in quakes_recent.get("quakes", []) if q.get("mag", 0) >= 6.0),
            "m7_plus_30d": sum(1 for q in quakes_recent.get("quakes", []) if q.get("mag", 0) >= 7.0),
        },
        "projections": {
            "atlantic_storms": storm_proj,
            "quakes_m5": quake_m5,
            "quakes_m6": quake_m6,
            "quakes_m7": quake_m7,
            "wildfires": fire_proj,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "7053"))
    log.info("Starting disasters dashboard on :%d", port)
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "0.0.0.0"), port=port)
