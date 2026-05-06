#!/usr/bin/env python3
"""Major Disasters Dashboard - FastAPI backend.

Live disaster tracking + Polymarket edge for disaster prediction markets.

Surfaces (as of v0.3):
  - Active threats: NHC + NRL ATCF tropical cyclones (all WMO basins),
    NWS severe-weather alerts (with flood subset), EONET open events
    (wildfires/severeStorms/volcanoes/floods), recent USGS earthquakes
    (M5+) plus PAGER significant events, GDACS red/orange events globally,
    Smithsonian GVP weekly volcanic-activity bulletin, SPC daily storm
    reports, SPC convective outlooks D1-D3, NOAA tsunami unified feed,
    ReliefWeb humanitarian disasters, AirNow metro AQI (key-gated).
  - Year-end projections via NB(mu, alpha) overdispersion model (replacing
    plain Poisson where empirically warranted): Atlantic named/hurricane/
    major-hurricane counts, global M5/M6/M7+ quakes, NIFC wildfire acres
    (Normal), EONET wildfire counts, US tornadoes, FEMA major-disaster (DR)
    declarations.
  - Map view: GeoJSON FeatureCollection of every active threat with
    severity/category metadata for the SVG map renderer.
  - Drought & impact: USDM categorical percentages, USGS PAGER alert level
    + felt reports + tsunami flag, ReliefWeb humanitarian impact.
  - Polymarket markets joined with model probabilities + edges + 1/4-Kelly
    position size + Polymarket deep-link.
  - Backtest panel: each year-end count projection replayed against the
    realised actual for the last 10 completed years.
  - Per-source health monitor: ``/api/sources`` shows status (GREEN/YELLOW/
    RED), latency EMA, and last-ok age for every upstream feed.
  - Disk-persisted cache: YTD counts, climo priors, and last-known-good
    projections survive process restarts.
  - Background pre-fetch loop (opt-in via DISASTERS_PREFETCH=1): walks 18
    upstreams on staggered schedules so page loads return instantly.

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
from fastapi.staticfiles import StaticFiles

from analysis import backtest as backtest_mod
from analysis import map_features as map_features_mod
from analysis import market_matcher
from analysis.negbin import nb_quantile_band
from ingestion import (
    _background,
    _health,
    _persistence,
    airnow_aqi,
    eonet_events,
    fema_declarations,
    gdacs_alerts,
    jtwc_pacific,
    nhc_storms,
    nifc_fires,
    nws_alerts,
    nws_floods,
    polymarket_client,
    reliefweb_disasters,
    smithsonian_volcanoes,
    spc_outlook,
    spc_tornadoes,
    tsunami_warnings,
    usdm_drought,
    usgs_quakes,
    usgs_significant,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("disasters")

app = FastAPI(title="Major Disasters Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
    return JSONResponse(usgs_quakes.recent_quakes(
        min_magnitude=max(0.0, min(min_magnitude, 9.0)),
        days=max(1, min(days, 365))))


@app.get("/api/quakes/projection")
async def api_quakes_projection(min_magnitude: float = 5.0) -> JSONResponse:
    proj = usgs_quakes.year_end_projection(min_magnitude=max(4.0, min(min_magnitude, 8.0)))
    return JSONResponse(_attach_band(proj, alpha_key=_quake_alpha_key(min_magnitude)))


def _quake_alpha_key(min_mag: float) -> str:
    if min_mag <= 5.5:
        return "global_m5"
    if min_mag <= 6.5:
        return "global_m6"
    return "global_m7"


def _attach_band(proj: dict, *, alpha_key: str) -> dict:
    """Attach an 80% / 95% credible interval to a projection."""
    if not proj or proj.get("error"):
        return proj
    mu = (proj.get("projected_year_end_count")
          or proj.get("projected_year_end_dr_count"))
    if mu is None:
        return proj
    from analysis.negbin import ALPHA
    alpha = ALPHA.get(alpha_key, 0.0)
    band80 = nb_quantile_band(float(mu), alpha, ci=0.80)
    band95 = nb_quantile_band(float(mu), alpha, ci=0.95)
    return {
        **proj,
        "alpha": alpha,
        "ci_80": band80,
        "ci_95": band95,
    }


@app.get("/api/quakes/significant")
async def api_quakes_significant(window: str = "month") -> JSONResponse:
    if window not in {"week", "month"}:
        window = "month"
    return JSONResponse(usgs_significant.significant_recent(window))


# ─── Tropical cyclones ────────────────────────────────────────────────────────

@app.get("/api/storms")
async def api_storms() -> JSONResponse:
    nhc = nhc_storms.active_storms()
    nrl = jtwc_pacific.active_storms_all_basins()
    return JSONResponse({
        "nhc": nhc,
        "nrl_all_basins": nrl,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/storms/projection")
async def api_storms_projection() -> JSONResponse:
    proj = nhc_storms.atlantic_season_projection()
    return JSONResponse(_attach_band(proj, alpha_key="atlantic_named_storms"))


# ─── NWS alerts (general + flood subset) ──────────────────────────────────────

@app.get("/api/alerts")
async def api_alerts(severity: str = "Severe") -> JSONResponse:
    return JSONResponse(nws_alerts.active_alerts(severity=severity))


@app.get("/api/floods")
async def api_floods() -> JSONResponse:
    return JSONResponse(nws_floods.active_flood_alerts())


# ─── EONET ────────────────────────────────────────────────────────────────────

@app.get("/api/eonet")
async def api_eonet(category: str = "all") -> JSONResponse:
    return JSONResponse(eonet_events.open_events(category=category))


@app.get("/api/eonet/projection")
async def api_eonet_projection(category: str = "wildfires") -> JSONResponse:
    return JSONResponse(eonet_events.year_end_count_projection(category=category))


# ─── GDACS ────────────────────────────────────────────────────────────────────

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
    proj = spc_tornadoes.ytd_tornado_projection()
    return JSONResponse(_attach_band(proj, alpha_key="us_tornadoes"))


@app.get("/api/spc/outlooks")
async def api_spc_outlooks() -> JSONResponse:
    return JSONResponse(spc_outlook.outlooks())


# ─── Volcanoes ────────────────────────────────────────────────────────────────

@app.get("/api/volcanoes")
async def api_volcanoes() -> JSONResponse:
    return JSONResponse(smithsonian_volcanoes.weekly_active())


# ─── Drought ──────────────────────────────────────────────────────────────────

@app.get("/api/drought")
async def api_drought(aoi: str = "conus") -> JSONResponse:
    if aoi not in {"conus", "total"}:
        aoi = "conus"
    return JSONResponse(usdm_drought.latest_categorical(aoi=aoi))


# ─── Tsunami ──────────────────────────────────────────────────────────────────

@app.get("/api/tsunami")
async def api_tsunami() -> JSONResponse:
    return JSONResponse(tsunami_warnings.active_warnings())


# ─── ReliefWeb ────────────────────────────────────────────────────────────────

@app.get("/api/reliefweb")
async def api_reliefweb(limit: int = 30) -> JSONResponse:
    return JSONResponse(reliefweb_disasters.ongoing_disasters(limit=max(1, min(limit, 100))))


# ─── AirNow ───────────────────────────────────────────────────────────────────

@app.get("/api/aqi")
async def api_aqi() -> JSONResponse:
    return JSONResponse(airnow_aqi.metro_aqi())


# ─── FEMA ─────────────────────────────────────────────────────────────────────

@app.get("/api/fema/recent")
async def api_fema_recent(days: int = 30) -> JSONResponse:
    return JSONResponse(fema_declarations.recent_declarations(days=max(1, min(days, 365))))


@app.get("/api/fema/projection")
async def api_fema_projection() -> JSONResponse:
    proj = fema_declarations.ytd_count_projection()
    return JSONResponse(_attach_band(proj, alpha_key="fema_dr"))


# ─── Polymarket markets ───────────────────────────────────────────────────────

def _fetch_all_projections() -> dict:
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
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


def _count_by_model(markets: list[dict]) -> dict:
    out: dict[str, int] = {}
    for m in markets:
        key = m.get("_model_used") or "unscored"
        out[key] = out.get(key, 0) + 1
    return out


# ─── Map / GeoJSON ────────────────────────────────────────────────────────────

@app.get("/api/map_features")
async def api_map_features() -> JSONResponse:
    return JSONResponse(map_features_mod.build())


# ─── Backtest ─────────────────────────────────────────────────────────────────

@app.get("/api/backtest")
async def api_backtest(n_years: int = 10) -> JSONResponse:
    n_years = max(1, min(n_years, 15))
    return JSONResponse({
        "atlantic_storms": backtest_mod.atlantic_storm_backtest(n_years=n_years),
        "wildfire_acres": backtest_mod.wildfire_acres_backtest(n_years=n_years),
        "method": backtest_mod.methodology(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Source health monitor ────────────────────────────────────────────────────

@app.get("/api/sources")
async def api_sources() -> JSONResponse:
    return JSONResponse({
        "sources": _health.all_sources(),
        "persisted_cache": _persistence.all_entries(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Single-shot summary ──────────────────────────────────────────────────────

async def _to_thread(fn, *args, **kwargs):
    """Run a sync ingestion call on the default executor so /api/summary can
    fan out to ~20 upstreams concurrently without blocking the event loop."""
    return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args, **kwargs))


@app.get("/api/summary")
async def api_summary() -> JSONResponse:
    """Single payload for the front page. Fans out to every ingestion module
    in parallel via ``asyncio.gather`` so a slow upstream doesn't dominate
    page-load latency."""
    (storms_nhc, storms_nrl, quakes_recent, eonet, alerts, floods, gdacs,
     fires_active, fires_proj, storm_proj, quake_m5, quake_m6, quake_m7,
     fire_count_proj, tornado_proj, tornadoes_today, fema_proj, drought,
     volcanoes, sig_quakes, tsunami, reliefweb, aqi, spc_out) = await asyncio.gather(
        _to_thread(nhc_storms.active_storms),
        _to_thread(jtwc_pacific.active_storms_all_basins),
        _to_thread(usgs_quakes.recent_quakes, 5.0, 30),
        _to_thread(eonet_events.open_events, "all"),
        _to_thread(nws_alerts.active_alerts, "Severe"),
        _to_thread(nws_floods.active_flood_alerts),
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
        _to_thread(tsunami_warnings.active_warnings),
        _to_thread(reliefweb_disasters.ongoing_disasters, 20),
        _to_thread(airnow_aqi.metro_aqi),
        _to_thread(spc_outlook.outlooks),
    )

    storm_proj = _attach_band(storm_proj, alpha_key="atlantic_named_storms")
    quake_m5 = _attach_band(quake_m5, alpha_key="global_m5")
    quake_m6 = _attach_band(quake_m6, alpha_key="global_m6")
    quake_m7 = _attach_band(quake_m7, alpha_key="global_m7")
    tornado_proj = _attach_band(tornado_proj, alpha_key="us_tornadoes")
    fema_proj = _attach_band(fema_proj, alpha_key="fema_dr")

    return JSONResponse({
        "active": {
            "named_storms": storms_nhc.get("storms", []),
            "named_storms_count": len(storms_nhc.get("storms", [])),
            "all_basin_storms": storms_nrl.get("storms", []),
            "all_basin_storm_count": storms_nrl.get("count", 0),
            "alerts_count": alerts.get("count", 0),
            "alerts_top": alerts.get("alerts", [])[:5],
            "alerts_by_event": alerts.get("by_event", {}),
            "flood_alerts": floods.get("count", 0),
            "flash_flood_alerts": floods.get("flash_flood_count", 0),
            "storm_surge_alerts": floods.get("storm_surge_count", 0),
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
            "tsunami_active": tsunami.get("count", 0),
            "tsunami_by_severity": tsunami.get("by_severity", {}),
            "tsunami_top": tsunami.get("entries", [])[:5],
            "reliefweb_count": reliefweb.get("count", 0),
            "reliefweb_top_countries": reliefweb.get("top_countries", {}),
            "spc_outlook": {
                "horizon_highest": spc_out.get("horizon_highest_category"),
                "horizon_day": spc_out.get("horizon_highest_day"),
                "day1": spc_out.get("days", {}).get("day1"),
            },
            "aqi_metros": aqi.get("metros", []),
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


# ─── Background pre-fetch loop ────────────────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    _background.start([
        ("nhc_active",           lambda: nhc_storms.active_storms(),                          600),
        ("nrl_active_tc",        lambda: jtwc_pacific.active_storms_all_basins(),             900),
        ("usgs_quakes_recent",   lambda: usgs_quakes.recent_quakes(5.0, 30),                  300),
        ("usgs_quakes_m5_proj",  lambda: usgs_quakes.year_end_projection(5.0),                900),
        ("usgs_quakes_m6_proj",  lambda: usgs_quakes.year_end_projection(6.0),                900),
        ("usgs_quakes_m7_proj",  lambda: usgs_quakes.year_end_projection(7.0),                900),
        ("usgs_significant",     lambda: usgs_significant.significant_recent("month"),        600),
        ("eonet_open_all",       lambda: eonet_events.open_events("all"),                     600),
        ("eonet_wildfires_proj", lambda: eonet_events.year_end_count_projection("wildfires"), 1800),
        ("nifc_active",          lambda: nifc_fires.active_incidents(),                       900),
        ("nifc_acres_proj",      lambda: nifc_fires.acres_burned_year_end_projection(),       900),
        ("nws_severe",           lambda: nws_alerts.active_alerts("Severe"),                  180),
        ("nws_floods",           lambda: nws_floods.active_flood_alerts(),                    180),
        ("gdacs_orange",         lambda: gdacs_alerts.active_events("Orange"),                900),
        ("spc_today",            lambda: spc_tornadoes.daily_storm_reports(),                 900),
        ("spc_outlooks",         lambda: spc_outlook.outlooks(),                              1800),
        ("smithsonian_volcanoes", lambda: smithsonian_volcanoes.weekly_active(),              43200),
        ("tsunami_active",       lambda: tsunami_warnings.active_warnings(),                  300),
        ("reliefweb_disasters",  lambda: reliefweb_disasters.ongoing_disasters(60),           3600),
        ("usdm_drought",         lambda: usdm_drought.latest_categorical("conus"),            43200),
        ("fema_recent",          lambda: fema_declarations.recent_declarations(30),           3600),
        ("fema_proj",            lambda: fema_declarations.ytd_count_projection(),            3600),
        ("polymarket_disasters", lambda: polymarket_client.fetch_disaster_markets(),          300),
    ])


@app.on_event("shutdown")
async def _shutdown() -> None:
    _background.stop()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "7053"))
    log.info("Starting disasters dashboard on :%d", port)
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "0.0.0.0"), port=port)
