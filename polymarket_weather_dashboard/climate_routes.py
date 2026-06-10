"""Climate section of the weather dashboard — Flask blueprint.

Ported from the retired standalone climate-dashboard (Flask, port 7052)
when it was merged into this service. Data sources, models, and methodology
live untouched in the vendored ``climate_app`` package (renamed from the
original ``app`` package — it uses relative imports, so only the name
changed); this module is only routes + JSON shaping, namespaced under
``/api/climate/*``. The climate UI is served at ``/climate`` (see
server.py).

Auth is attached by server.py via ``blueprint.before_request`` — the
standalone climate dashboard had no auth at all, so the merge is a strict
security upgrade.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify

from climate_app.fetchers import co2 as co2_src
from climate_app.fetchers import gistemp as gistemp_src
from climate_app.fetchers import methane as methane_src
from climate_app.fetchers import n2o as n2o_src
from climate_app.fetchers import oni as oni_src
from climate_app.fetchers import polymarket as polymarket_src
from climate_app.fetchers import sea_ice as sea_ice_src
from climate_app.fetchers import sst as sst_src
from climate_app.methodology import payload as methodology_payload
from climate_app.models import co2 as co2_model
from climate_app.models import calibration
from climate_app.models import markets as markets_model
from climate_app.models import methane as methane_model
from climate_app.models import n2o as n2o_model
from climate_app.models import sea_ice as sea_ice_model
from climate_app.models import temperature as temperature_model

climate_bp = Blueprint("climate", __name__, url_prefix="/api/climate")


@climate_bp.get("/health")
def api_health():
    return jsonify({"ok": True, "service": "weather-dashboard/climate", "ts": time.time()})


@climate_bp.get("/methodology")
def api_methodology():
    return jsonify(methodology_payload(commit=None))


# ─── Per-source endpoints ──────────────────────────────────────────────────────

@climate_bp.get("/temperature")
def api_temperature():
    g = gistemp_src.fetch()
    if not g:
        return jsonify({"error": "GISTEMP fetch failed"}), 503
    return jsonify({**g, "projection": temperature_model.projection(g)})


@climate_bp.get("/co2")
def api_co2():
    c = co2_src.fetch()
    if not c:
        return jsonify({"error": "CO2 fetch failed"}), 503
    return jsonify({**c, "projection": co2_model.projection(c)})


@climate_bp.get("/methane")
def api_methane():
    m = methane_src.fetch()
    if not m:
        return jsonify({"error": "Methane fetch failed"}), 503
    proj = methane_model.projection(m)
    return jsonify({**m, "projection": proj,
                    "thresholds": methane_model.threshold_probs(proj)})


@climate_bp.get("/n2o")
def api_n2o():
    n = n2o_src.fetch()
    if not n:
        return jsonify({"error": "N2O fetch failed"}), 503
    proj = n2o_model.projection(n)
    return jsonify({**n, "projection": proj,
                    "thresholds": n2o_model.threshold_probs(proj)})


@climate_bp.get("/sea-ice")
def api_sea_ice():
    s = sea_ice_src.fetch()
    if not s:
        return jsonify({"error": "Sea ice fetch failed"}), 503
    arctic = s.get("arctic") or []
    antarctic = s.get("antarctic") or []
    return jsonify({
        "source": s["source"],
        "units": s["units"],
        "fetched_at": s["fetched_at"],
        "arctic_recent": arctic[-1100:],
        "antarctic_recent": antarctic[-1100:],
        "arctic_annual": sea_ice_model.annual_extremes(arctic),
        "antarctic_annual": sea_ice_model.annual_extremes(antarctic),
        "record_check": sea_ice_model.daily_record_check(s),
    })


@climate_bp.get("/sst")
def api_sst():
    s = sst_src.fetch()
    if not s:
        return jsonify({"error": "SST fetch failed"}), 503
    return jsonify(s)


@climate_bp.get("/regime")
def api_regime():
    o = oni_src.fetch()
    if not o:
        return jsonify({"error": "ONI fetch failed"}), 503
    return jsonify(o)


# ─── Aggregate endpoints ───────────────────────────────────────────────────────

@climate_bp.get("/markets")
def api_markets():
    markets = polymarket_src.fetch()
    g = gistemp_src.fetch()
    c = co2_src.fetch()
    s = sea_ice_src.fetch()
    ch4 = methane_src.fetch()
    gp = temperature_model.projection(g) if g else None
    cp = co2_model.projection(c) if c else None
    ap = sea_ice_model.arctic_min_projection(s) if s else None
    aap = sea_ice_model.antarctic_min_projection(s) if s else None
    mp = methane_model.projection(ch4) if ch4 else None
    enriched = markets_model.edges_for_markets(markets, gp, cp, ap, aap, mp)
    return jsonify({
        "markets": enriched,
        "count": len(enriched),
        "gistemp_projection": gp,
        "co2_projection": cp,
        "methane_projection": mp,
        "arctic_min_projection": ap,
        "antarctic_min_projection": aap,
        "temperature_thresholds": temperature_model.threshold_probs(gp),
        "co2_thresholds": co2_model.threshold_probs(cp),
        "methane_thresholds": methane_model.threshold_probs(mp),
    })


@climate_bp.get("/summary")
def api_summary():
    """Single endpoint giving the climate front page everything in one shot."""
    g = gistemp_src.fetch()
    c = co2_src.fetch()
    s = sea_ice_src.fetch()
    o = oni_src.fetch()
    ch4 = methane_src.fetch()
    n2o = n2o_src.fetch()
    gp = temperature_model.projection(g) if g else None
    cp = co2_model.projection(c) if c else None
    ap = sea_ice_model.arctic_min_projection(s) if s else None
    aap = sea_ice_model.antarctic_min_projection(s) if s else None
    mp = methane_model.projection(ch4) if ch4 else None
    np_ = n2o_model.projection(n2o) if n2o else None
    return jsonify({
        "gistemp": {
            "latest_annual": g["annual"][-1] if g and g.get("annual") else None,
            "projection": gp,
            "thresholds": temperature_model.threshold_probs(gp),
            "calibration": calibration.summary(
                temperature_model.backtest(g) if g else [], "error_c", "°C"),
        },
        "co2": {
            "latest": c["latest"] if c else None,
            "projection": cp,
            "thresholds": co2_model.threshold_probs(cp),
            "calibration": calibration.summary(
                co2_model.backtest(c) if c else [], "error_ppm", "ppm"),
        },
        "methane": {
            "latest": ch4["latest"] if ch4 else None,
            "projection": mp,
            "thresholds": methane_model.threshold_probs(mp),
            "calibration": calibration.summary(
                methane_model.backtest(ch4) if ch4 else [], "error_ppb", "ppb"),
        },
        "n2o": {
            "latest": n2o["latest"] if n2o else None,
            "projection": np_,
            "thresholds": n2o_model.threshold_probs(np_),
            "calibration": calibration.summary(
                n2o_model.backtest(n2o) if n2o else [], "error_ppb", "ppb"),
        },
        "sea_ice": {
            "record_check": sea_ice_model.daily_record_check(s) if s else None,
            "arctic_projection": ap,
            "antarctic_projection": aap,
        },
        "regime": {
            "latest": o["latest"] if o else None,
            "state": o["state"] if o else None,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@climate_bp.get("/backtest")
def api_backtest():
    g = gistemp_src.fetch()
    c = co2_src.fetch()
    ch4 = methane_src.fetch()
    n2o = n2o_src.fetch()
    gist_rows = temperature_model.backtest(g) if g else []
    co2_rows = co2_model.backtest(c) if c else []
    ch4_rows = methane_model.backtest(ch4) if ch4 else []
    n2o_rows = n2o_model.backtest(n2o) if n2o else []
    return jsonify({
        "gistemp": gist_rows,
        "co2": co2_rows,
        "methane": ch4_rows,
        "n2o": n2o_rows,
        "calibration": {
            "gistemp": calibration.summary(gist_rows, "error_c", "°C"),
            "co2": calibration.summary(co2_rows, "error_ppm", "ppm"),
            "methane": calibration.summary(ch4_rows, "error_ppb", "ppb"),
            "n2o": calibration.summary(n2o_rows, "error_ppb", "ppb"),
        },
        "method": {
            "gistemp": "Replays the YTD-anomaly + historical-drift model 'as of June' for each year, scored vs the actual J-D mean.",
            "co2": "Refits the 24-month linear regression at June of each year, scored vs the actual December reading.",
            "methane": "Same June-cutoff 24-month regression as CO₂, scored vs the actual December reading.",
            "n2o": "Same June-cutoff 24-month regression as CO₂/CH₄, scored vs the actual December reading.",
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })
