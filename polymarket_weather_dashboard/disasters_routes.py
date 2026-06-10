"""Disasters section of the weather dashboard — Flask blueprint.

Ported from the retired standalone disasters-dashboard (FastAPI, port 7053)
when it was merged into this service. The data layer lives untouched in the
vendored ``ingestion``/``analysis`` packages; this module is only the route
table, namespaced under ``/api/disasters/*`` so nothing collides with the
weather API. The disasters UI is served at ``/disasters`` (see server.py)
and fetches exclusively from this prefix.

Auth is attached by server.py via ``blueprint.before_request`` so the same
gateway-SSO rules cover weather, disasters, and climate endpoints alike.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from analysis import backtest as backtest_mod
from analysis import calibration as calibration_mod
from analysis import cross_venue as cross_venue_mod
from analysis import map_features as map_features_mod
from analysis import market_matcher
from analysis.negbin import ALPHA, nb_quantile_band
from ingestion import (
    _background,
    _health,
    _persistence,
    airnow_aqi,
    eonet_events,
    fema_declarations,
    gdacs_alerts,
    jtwc_pacific,
    kalshi_disasters,
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

log = logging.getLogger("weather.disasters")

disasters_bp = Blueprint("disasters", __name__, url_prefix="/api/disasters")


def _arg_float(name: str, default: float) -> float:
    try:
        return float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _arg_int(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


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
    alpha = ALPHA.get(alpha_key, 0.0)
    band80 = nb_quantile_band(float(mu), alpha, ci=0.80)
    band95 = nb_quantile_band(float(mu), alpha, ci=0.95)
    return {
        **proj,
        "alpha": alpha,
        "ci_80": band80,
        "ci_95": band95,
    }


@disasters_bp.get("/health")
def api_health():
    return jsonify({"ok": True, "service": "weather-dashboard/disasters", "ts": datetime.now(timezone.utc).timestamp()})


# ─── Earthquakes ──────────────────────────────────────────────────────────────

@disasters_bp.get("/quakes")
def api_quakes():
    min_magnitude = _arg_float("min_magnitude", 5.0)
    days = _arg_int("days", 30)
    return jsonify(usgs_quakes.recent_quakes(
        min_magnitude=max(0.0, min(min_magnitude, 9.0)),
        days=max(1, min(days, 365))))


@disasters_bp.get("/quakes/projection")
def api_quakes_projection():
    min_magnitude = _arg_float("min_magnitude", 5.0)
    proj = usgs_quakes.year_end_projection(min_magnitude=max(4.0, min(min_magnitude, 8.0)))
    return jsonify(_attach_band(proj, alpha_key=_quake_alpha_key(min_magnitude)))


@disasters_bp.get("/quakes/significant")
def api_quakes_significant():
    window = request.args.get("window", "month")
    if window not in {"week", "month"}:
        window = "month"
    return jsonify(usgs_significant.significant_recent(window))


# ─── Tropical cyclones ────────────────────────────────────────────────────────

@disasters_bp.get("/storms")
def api_storms():
    nhc = nhc_storms.active_storms()
    nrl = jtwc_pacific.active_storms_all_basins()
    return jsonify({
        "nhc": nhc,
        "nrl_all_basins": nrl,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@disasters_bp.get("/storms/projection")
def api_storms_projection():
    proj = nhc_storms.atlantic_season_projection()
    return jsonify(_attach_band(proj, alpha_key="atlantic_named_storms"))


# ─── NWS alerts (general + flood subset) ──────────────────────────────────────

@disasters_bp.get("/alerts")
def api_alerts():
    return jsonify(nws_alerts.active_alerts(severity=request.args.get("severity", "Severe")))


@disasters_bp.get("/floods")
def api_floods():
    return jsonify(nws_floods.active_flood_alerts())


# ─── EONET ────────────────────────────────────────────────────────────────────

@disasters_bp.get("/eonet")
def api_eonet():
    return jsonify(eonet_events.open_events(category=request.args.get("category", "all")))


@disasters_bp.get("/eonet/projection")
def api_eonet_projection():
    return jsonify(eonet_events.year_end_count_projection(
        category=request.args.get("category", "wildfires")))


# ─── GDACS ────────────────────────────────────────────────────────────────────

@disasters_bp.get("/gdacs")
def api_gdacs():
    return jsonify(gdacs_alerts.active_events(min_alert=request.args.get("min_alert", "Orange")))


# ─── NIFC wildfires ───────────────────────────────────────────────────────────

@disasters_bp.get("/fires/active")
def api_fires_active():
    return jsonify(nifc_fires.active_incidents())


@disasters_bp.get("/fires/projection")
def api_fires_projection():
    return jsonify(nifc_fires.acres_burned_year_end_projection())


# ─── Tornadoes ────────────────────────────────────────────────────────────────

@disasters_bp.get("/tornadoes")
def api_tornadoes():
    return jsonify(spc_tornadoes.daily_storm_reports())


@disasters_bp.get("/tornadoes/projection")
def api_tornadoes_projection():
    proj = spc_tornadoes.ytd_tornado_projection()
    return jsonify(_attach_band(proj, alpha_key="us_tornadoes"))


@disasters_bp.get("/spc/outlooks")
def api_spc_outlooks():
    return jsonify(spc_outlook.outlooks())


# ─── Volcanoes ────────────────────────────────────────────────────────────────

@disasters_bp.get("/volcanoes")
def api_volcanoes():
    return jsonify(smithsonian_volcanoes.weekly_active())


# ─── Drought ──────────────────────────────────────────────────────────────────

@disasters_bp.get("/drought")
def api_drought():
    aoi = request.args.get("aoi", "conus")
    if aoi not in {"conus", "total"}:
        aoi = "conus"
    return jsonify(usdm_drought.latest_categorical(aoi=aoi))


# ─── Tsunami ──────────────────────────────────────────────────────────────────

@disasters_bp.get("/tsunami")
def api_tsunami():
    return jsonify(tsunami_warnings.active_warnings())


# ─── ReliefWeb ────────────────────────────────────────────────────────────────

@disasters_bp.get("/reliefweb")
def api_reliefweb():
    limit = _arg_int("limit", 30)
    return jsonify(reliefweb_disasters.ongoing_disasters(limit=max(1, min(limit, 100))))


# ─── AirNow ───────────────────────────────────────────────────────────────────

@disasters_bp.get("/aqi")
def api_aqi():
    return jsonify(airnow_aqi.metro_aqi())


# ─── FEMA ─────────────────────────────────────────────────────────────────────

@disasters_bp.get("/fema/recent")
def api_fema_recent():
    days = _arg_int("days", 30)
    return jsonify(fema_declarations.recent_declarations(days=max(1, min(days, 365))))


@disasters_bp.get("/fema/projection")
def api_fema_projection():
    proj = fema_declarations.ytd_count_projection()
    return jsonify(_attach_band(proj, alpha_key="fema_dr"))


# ─── Kalshi disaster markets ──────────────────────────────────────────────────

@disasters_bp.get("/kalshi")
def api_kalshi():
    return jsonify(kalshi_disasters.fetch_disaster_markets())


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


@disasters_bp.get("/markets")
def api_markets():
    markets = polymarket_client.fetch_disaster_markets()
    projs = _fetch_all_projections()
    enriched = market_matcher.enrich_markets(markets, **projs)
    return jsonify({
        "markets": enriched,
        "count": len(enriched),
        "scored_count": sum(1 for m in enriched if m.get("_model_p") is not None),
        "by_model": _count_by_model(enriched),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@disasters_bp.get("/markets/crossvenue")
def api_markets_crossvenue():
    """Polymarket + Kalshi joined on (topic, threshold). Sorted by absolute
    arb spread descending; rows where one venue is missing surface too so
    the table can show one-venue markets."""
    poly = polymarket_client.fetch_disaster_markets()
    kalshi = kalshi_disasters.fetch_disaster_markets()
    joined = cross_venue_mod.join_markets(poly, kalshi.get("markets") or [])
    return jsonify({
        **joined,
        "poly_count": len(poly),
        "kalshi_count": kalshi.get("count", 0),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


def _count_by_model(markets: list[dict]) -> dict:
    out: dict[str, int] = {}
    for m in markets:
        key = m.get("_model_used") or "unscored"
        out[key] = out.get(key, 0) + 1
    return out


# ─── Calibration scorecard ────────────────────────────────────────────────────

@disasters_bp.get("/signals/calibration")
def api_calibration():
    return jsonify({
        **calibration_mod.report(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Map / GeoJSON ────────────────────────────────────────────────────────────

@disasters_bp.get("/map_features")
def api_map_features():
    return jsonify(map_features_mod.build())


# ─── Backtest ─────────────────────────────────────────────────────────────────

@disasters_bp.get("/backtest")
def api_backtest():
    n_years = max(1, min(_arg_int("n_years", 10), 15))
    return jsonify({
        "atlantic_storms": backtest_mod.atlantic_storm_backtest(n_years=n_years),
        "wildfire_acres": backtest_mod.wildfire_acres_backtest(n_years=n_years),
        "method": backtest_mod.methodology(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Source health monitor ────────────────────────────────────────────────────

@disasters_bp.get("/sources")
def api_sources():
    return jsonify({
        "sources": _health.all_sources(),
        "persisted_cache": _persistence.all_entries(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Single-shot summary ──────────────────────────────────────────────────────

@disasters_bp.get("/summary")
def api_summary():
    """Single payload for the disasters front page. Fans out to every
    ingestion module on a thread pool so a slow upstream doesn't dominate
    page-load latency (the Flask port of the old asyncio.gather)."""
    calls = [
        lambda: nhc_storms.active_storms(),
        lambda: jtwc_pacific.active_storms_all_basins(),
        lambda: usgs_quakes.recent_quakes(5.0, 30),
        lambda: eonet_events.open_events("all"),
        lambda: nws_alerts.active_alerts("Severe"),
        lambda: nws_floods.active_flood_alerts(),
        lambda: gdacs_alerts.active_events("Orange"),
        lambda: nifc_fires.active_incidents(),
        lambda: nifc_fires.acres_burned_year_end_projection(),
        lambda: nhc_storms.atlantic_season_projection(),
        lambda: usgs_quakes.year_end_projection(5.0),
        lambda: usgs_quakes.year_end_projection(6.0),
        lambda: usgs_quakes.year_end_projection(7.0),
        lambda: eonet_events.year_end_count_projection("wildfires"),
        lambda: spc_tornadoes.ytd_tornado_projection(),
        lambda: spc_tornadoes.daily_storm_reports(),
        lambda: fema_declarations.ytd_count_projection(),
        lambda: usdm_drought.latest_categorical("conus"),
        lambda: smithsonian_volcanoes.weekly_active(),
        lambda: usgs_significant.significant_recent("month"),
        lambda: tsunami_warnings.active_warnings(),
        lambda: reliefweb_disasters.ongoing_disasters(20),
        lambda: airnow_aqi.metro_aqi(),
        lambda: spc_outlook.outlooks(),
    ]
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        (storms_nhc, storms_nrl, quakes_recent, eonet, alerts, floods, gdacs,
         fires_active, fires_proj, storm_proj, quake_m5, quake_m6, quake_m7,
         fire_count_proj, tornado_proj, tornadoes_today, fema_proj, drought,
         volcanoes, sig_quakes, tsunami, reliefweb, aqi, spc_out) = list(
            pool.map(lambda fn: fn(), calls))

    storm_proj = _attach_band(storm_proj, alpha_key="atlantic_named_storms")
    quake_m5 = _attach_band(quake_m5, alpha_key="global_m5")
    quake_m6 = _attach_band(quake_m6, alpha_key="global_m6")
    quake_m7 = _attach_band(quake_m7, alpha_key="global_m7")
    tornado_proj = _attach_band(tornado_proj, alpha_key="us_tornadoes")
    fema_proj = _attach_band(fema_proj, alpha_key="fema_dr")

    return jsonify({
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

def start_prefetch() -> None:
    """Start the disasters background pre-fetch loop (opt-in).

    Same job table the standalone dashboard used; gated on
    ``DISASTERS_PREFETCH=1`` inside ``_background.start`` so smoke tests
    never hit live upstreams.
    """
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
        ("kalshi_disasters",     lambda: kalshi_disasters.fetch_disaster_markets(),           300),
    ])
