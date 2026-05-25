#!/usr/bin/env python3
"""Polymarket Climate Change Dashboard — Flask routes.

Long-horizon climate markets: warmest year on record, Arctic + Antarctic sea
ice extent, global mean temperature anomaly, atmospheric CO2 + CH4, sea
surface temperature, ENSO regime.

Data sources, models, and methodology live in the ``app`` package — this
file is intentionally only routes + JSON shaping. See ``app/methodology.py``
or ``GET /api/methodology`` for the model descriptions.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, send_from_directory

from app.fetchers import co2 as co2_src
from app.fetchers import gistemp as gistemp_src
from app.fetchers import gistemp_zonal as gistemp_zonal_src
from app.fetchers import kalshi as kalshi_src
from app.fetchers import methane as methane_src
from app.fetchers import n2o as n2o_src
from app.fetchers import ocean_heat as ocean_heat_src
from app.fetchers import oni as oni_src
from app.fetchers import owid_emissions as emissions_src
from app.fetchers import snow_cover as snow_cover_src
from app.fetchers import polymarket as polymarket_src
from app.fetchers import sea_ice as sea_ice_src
from app.fetchers import sea_level as sea_level_src
from app.fetchers import sf6 as sf6_src
from app.fetchers import sst as sst_src
from app.methodology import payload as methodology_payload
from app import snapshot as snapshot_module
from app import status as status_module
from app.models import calibration
from app.models import carbon_budget as carbon_budget_model
from app.models import co2 as co2_model
from app.models import emissions as emissions_model
from app.models import forcing as forcing_model
from app.models import highlights as highlights_model
from app.models import markets as markets_model
from app.models import methane as methane_model
from app.models import n2o as n2o_model
from app.models import scenarios as scenarios_model
from app.models import sea_ice as sea_ice_model
from app.models import sf6 as sf6_model
from app.models import temperature as temperature_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("climate")

app = Flask(__name__, static_folder="static")

try:
    from flask_compress import Compress
    Compress(app)
except Exception:
    logger.warning("flask_compress not available; responses will not be gzipped")

PORT = int(os.environ.get("PORT", "7052"))


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
    except Exception:
        return None


_COMMIT = _git_sha()


# ─── Static + health ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/methodology")
def methodology_page():
    return send_from_directory("static", "methodology.html")


@app.route("/status")
def status_page():
    return send_from_directory("static", "status.html")


# Map every upstream source to (display URL, fetch callable) for the status
# health-dashboard. Defined once so /api/status doesn't drift from the actual
# fetchers as we add new ones.
_STATUS_SOURCES = {
    "GISTEMP (NASA temperature)":           (gistemp_src.URL, gistemp_src.fetch, "gistemp"),
    "GISTEMP zonal (NASA, by latitude)":    (gistemp_zonal_src.URL, gistemp_zonal_src.fetch, "gistemp_zonal"),
    "CO₂ (NOAA Mauna Loa)":                 (co2_src.URL, co2_src.fetch, "co2"),
    "CH₄ (NOAA GML)":                       (methane_src.URL, methane_src.fetch, "methane"),
    "N₂O (NOAA GML)":                       (n2o_src.URL, n2o_src.fetch, "n2o"),
    "SF₆ (NOAA GML)":                       (sf6_src.URL, sf6_src.fetch, "sf6"),
    "Sea ice (NSIDC, both hemispheres)":    (sea_ice_src.URL_NORTH, sea_ice_src.fetch, "sea_ice"),
    "SST (Climate Reanalyzer / OISST)":     (sst_src.URL, sst_src.fetch, "sst"),
    "ONI (NOAA CPC ENSO)":                  (oni_src.URL, oni_src.fetch, "oni"),
    "Ocean heat content (NOAA NCEI)":       (ocean_heat_src.URL, ocean_heat_src.fetch, "ocean_heat"),
    "Sea level (NOAA STAR)":                (sea_level_src.URL, sea_level_src.fetch, "sea_level"),
    "NH snow cover (Rutgers)":              (snow_cover_src.URL, snow_cover_src.fetch, "snow_cover"),
    "Country emissions (OWID)":             (emissions_src.URL, emissions_src.fetch, "owid_emissions"),
    "Polymarket climate markets":           ("https://gamma-api.polymarket.com/events", polymarket_src.fetch, "polymarket"),
    "Kalshi climate markets":               (kalshi_src.URL, kalshi_src.fetch, "kalshi"),
}


@app.route("/api/status")
def api_status():
    """Per-source health snapshot — which upstream fetchers are succeeding."""
    return jsonify(status_module.compute(fetchers=_STATUS_SOURCES))


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "climate-dashboard",
                    "commit": _COMMIT, "ts": time.time()})


@app.route("/api/methodology")
def api_methodology():
    return jsonify(methodology_payload(commit=_COMMIT))


@app.route("/snapshot.txt")
def api_snapshot_text():
    """Plain-text dashboard snapshot suitable for posting / piping / cron."""
    g = gistemp_src.fetch()
    c = co2_src.fetch()
    ch4 = methane_src.fetch()
    n2o = n2o_src.fetch()
    sf6 = sf6_src.fetch()
    s = sea_ice_src.fetch()
    o = oni_src.fetch()
    em = emissions_src.fetch()
    forcing = forcing_model.compute(co2=c, methane=ch4, n2o=n2o, sf6=sf6)
    em_summary = None
    if em:
        em_summary = {
            "latest_year": em["latest_year"],
            "top_emitters": emissions_model.top_emitters(em, n=10),
        }
    hl = highlights_model.compute(
        gistemp=g, co2=c, methane=ch4, n2o=n2o, sea_ice=s, oni=o,
    )
    text = snapshot_module.text_snapshot(
        gistemp=g, co2=c, methane=ch4, n2o=n2o, sf6=sf6,
        sea_ice=s, oni=o, forcing=forcing, highlights=hl,
        emissions=em_summary,
    )
    return Response(text, mimetype="text/plain; charset=utf-8")


@app.route("/feed.xml")
def feed_xml():
    """RSS 2.0 feed. Default kind=highlights; ?kind=opportunities returns a
    feed of high-edge climate markets with optional &min_edge= and
    &min_liq= overrides for sensitivity tuning."""
    kind = (request.args.get("kind") or "highlights").lower()
    if kind == "opportunities":
        try:
            min_edge = float(request.args.get("min_edge") or 5.0)
        except ValueError:
            min_edge = 5.0
        try:
            min_liq = float(request.args.get("min_liq") or 500.0)
        except ValueError:
            min_liq = 500.0
        # Reuse the markets fetch + scoring from /api/markets
        markets = polymarket_src.fetch()
        g = gistemp_src.fetch()
        c = co2_src.fetch()
        ch4 = methane_src.fetch()
        n2o = n2o_src.fetch()
        sea = sea_ice_src.fetch()
        enriched = markets_model.edges_for_markets(
            markets,
            temperature_model.projection(g) if g else None,
            co2_model.projection(c) if c else None,
            sea_ice_model.arctic_min_projection(sea) if sea else None,
            sea_ice_model.antarctic_min_projection(sea) if sea else None,
            methane_model.projection(ch4) if ch4 else None,
            n2o_model.projection(n2o) if n2o else None,
        )
        xml = snapshot_module.opportunities_rss(enriched,
                                                 min_edge_pp=min_edge,
                                                 min_liquidity=min_liq)
        return Response(xml, mimetype="application/rss+xml; charset=utf-8")
    hl = highlights_model.compute(
        gistemp=gistemp_src.fetch(),
        co2=co2_src.fetch(),
        methane=methane_src.fetch(),
        n2o=n2o_src.fetch(),
        sea_ice=sea_ice_src.fetch(),
        oni=oni_src.fetch(),
    )
    xml = snapshot_module.rss_feed(hl)
    return Response(xml, mimetype="application/rss+xml; charset=utf-8")


@app.route("/api/highlights")
def api_highlights():
    """Auto-derived 'what's notable today' chips. Pure derivative of cached
    upstream data — no extra HTTP fetches."""
    return jsonify({
        "items": highlights_model.compute(
            gistemp=gistemp_src.fetch(),
            co2=co2_src.fetch(),
            methane=methane_src.fetch(),
            n2o=n2o_src.fetch(),
            sea_ice=sea_ice_src.fetch(),
            oni=oni_src.fetch(),
            zonal=gistemp_zonal_src.fetch(),
        ),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Per-source endpoints ──────────────────────────────────────────────────────

@app.route("/api/temperature")
def api_temperature():
    g = gistemp_src.fetch()
    if not g:
        return jsonify({"error": "GISTEMP fetch failed"}), 503
    return jsonify({**g, "projection": temperature_model.projection(g)})


@app.route("/api/zonal")
def api_zonal():
    """NASA GISTEMP zonal annual temperature — warming by latitude band."""
    z = gistemp_zonal_src.fetch()
    if not z:
        return jsonify({"error": "GISTEMP zonal fetch failed",
                        "url": gistemp_zonal_src.URL,
                        "hint": "Likely an upstream URL change at NASA GISS"}), 503
    return jsonify({**z,
                    "warming_ratios": gistemp_zonal_src.warming_ratios(z)})


@app.route("/api/co2")
def api_co2():
    c = co2_src.fetch()
    if not c:
        return jsonify({"error": "CO2 fetch failed"}), 503
    return jsonify({**c, "projection": co2_model.projection(c)})


@app.route("/api/methane")
def api_methane():
    m = methane_src.fetch()
    if not m:
        return jsonify({"error": "Methane fetch failed"}), 503
    proj = methane_model.projection(m)
    return jsonify({**m, "projection": proj,
                    "thresholds": methane_model.threshold_probs(proj)})


@app.route("/api/n2o")
def api_n2o():
    n = n2o_src.fetch()
    if not n:
        return jsonify({"error": "N2O fetch failed"}), 503
    proj = n2o_model.projection(n)
    return jsonify({**n, "projection": proj,
                    "thresholds": n2o_model.threshold_probs(proj)})


@app.route("/api/sf6")
def api_sf6():
    s = sf6_src.fetch()
    if not s:
        return jsonify({"error": "SF6 fetch failed"}), 503
    proj = sf6_model.projection(s)
    return jsonify({**s, "projection": proj,
                    "thresholds": sf6_model.threshold_probs(proj)})


@app.route("/api/ocean-heat")
def api_ocean_heat():
    """NOAA NCEI 0-2000m ocean heat content anomaly, yearly (10^22 J)."""
    o = ocean_heat_src.fetch()
    if not o:
        return jsonify({"error": "Ocean heat content fetch failed",
                        "url": ocean_heat_src.URL,
                        "hint": "Likely an upstream URL change at NCEI — see methodology"}), 503
    return jsonify(o)


@app.route("/api/sea-level")
def api_sea_level():
    """NOAA STAR satellite altimetry — global mean sea level in mm."""
    s = sea_level_src.fetch()
    if not s:
        return jsonify({"error": "Sea level fetch failed",
                        "url": sea_level_src.URL,
                        "hint": "Likely an upstream URL change at NESDIS"}), 503
    return jsonify(s)


@app.route("/api/snow-cover")
def api_snow_cover():
    """Rutgers Global Snow Lab — NH land snow cover, monthly (million km²)."""
    s = snow_cover_src.fetch()
    if not s:
        return jsonify({"error": "Snow cover fetch failed",
                        "url": snow_cover_src.URL,
                        "hint": "Likely an upstream URL change at Rutgers"}), 503
    return jsonify(s)


@app.route("/api/scenarios")
def api_scenarios():
    """IPCC AR6 SSP scenarios — temperature + CO₂ trajectories through 2100,
    plus a "which scenario is the dashboard's current reading closest to?"
    assessment for both metrics.

    Pure static data — no upstream fetch.
    """
    g = gistemp_src.fetch()
    c = co2_src.fetch()
    cur_year = datetime.now(timezone.utc).year
    temp_match = None
    co2_match = None
    if g and g.get("annual"):
        latest_anomaly = g["annual"][-1]["anomaly_c"]
        temp_match = scenarios_model.closest_temp_scenario(latest_anomaly, cur_year)
    if c and c.get("latest"):
        co2_match = scenarios_model.closest_co2_scenario(c["latest"]["ppm"], cur_year)
    return jsonify({
        "trajectories": {
            "temperature_c_vs_1850_1900": scenarios_model.all_trajectories("temp"),
            "co2_ppm": scenarios_model.all_trajectories("co2"),
        },
        "current_match": {
            "temperature": temp_match,
            "co2": co2_match,
        },
        "baseline_note": "Temperature trajectories use the IPCC AR6 baseline (1850-1900). GISTEMP uses 1951-1980 — we add ~0.2°C when comparing dashboard readings to scenarios.",
        "source": "IPCC AR6 WG1 Table SPM.1 (temperature) + SSP database (CO₂); anchor points only, linearly interpolated.",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/carbon-budget")
def api_carbon_budget():
    """Remaining carbon budget for 1.5°C / 2°C warming targets.

    IPCC AR6 anchor budgets (start of 2020) minus cumulative global CO₂
    emissions from OWID since then. Reports remaining GtCO₂ + years at
    the latest annual emission rate for each target.
    """
    parsed = emissions_src.fetch()
    if not parsed:
        return jsonify({"error": "Carbon budget needs OWID emissions data",
                        "hint": "Check /status — Country emissions (OWID)"}), 503
    payload = carbon_budget_model.compute(parsed)
    if not payload:
        return jsonify({"error": "World row missing from OWID data"}), 503
    return jsonify(payload)


@app.route("/api/emissions")
def api_emissions():
    """Country-level CO₂ emissions from Our World in Data.

    Returns top-N emitters (defaults to 10) for the latest year present in
    the upstream dataset, plus a global summary. We strip the per-country
    full time-series before responding to keep the payload small — that
    history is on the upstream URL if anyone wants it.
    """
    parsed = emissions_src.fetch()
    if not parsed:
        return jsonify({"error": "OWID emissions fetch failed"}), 503
    return jsonify({
        "source": parsed["source"],
        "latest_year": parsed["latest_year"],
        "top_emitters": emissions_model.top_emitters(parsed, n=10),
        "global": emissions_model.global_summary(parsed),
        "fetched_at": parsed["fetched_at"],
    })


@app.route("/api/forcing")
def api_forcing():
    """Combined GHG radiative forcing in W/m² above pre-industrial.

    No new HTTP fetches — composes CO₂ + CH₄ + N₂O + SF₆ data we already
    cache. Returns per-gas breakdown plus an "effective CO₂ ppm" framing
    (the CO₂ concentration alone that would produce the same forcing).
    """
    payload = forcing_model.compute(
        co2=co2_src.fetch(),
        methane=methane_src.fetch(),
        n2o=n2o_src.fetch(),
        sf6=sf6_src.fetch(),
    )
    if payload is None:
        return jsonify({"error": "CO2 data required for forcing calculation"}), 503
    return jsonify(payload)


@app.route("/api/sea-ice")
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


@app.route("/api/sst")
def api_sst():
    s = sst_src.fetch()
    if not s:
        return jsonify({"error": "SST fetch failed"}), 503
    return jsonify(s)


@app.route("/api/regime")
def api_regime():
    o = oni_src.fetch()
    if not o:
        return jsonify({"error": "ONI fetch failed"}), 503
    return jsonify(o)


# ─── Aggregate endpoints ───────────────────────────────────────────────────────

@app.route("/api/markets")
def api_markets():
    poly = polymarket_src.fetch() or []
    kalshi = kalshi_src.fetch() or []
    # Merge both venues — every market has _venue set so the frontend can
    # render a venue badge. Score both with the same model regex set.
    markets = list(poly) + list(kalshi)
    g = gistemp_src.fetch()
    c = co2_src.fetch()
    s = sea_ice_src.fetch()
    ch4 = methane_src.fetch()
    n2o = n2o_src.fetch()
    gp = temperature_model.projection(g) if g else None
    cp = co2_model.projection(c) if c else None
    ap = sea_ice_model.arctic_min_projection(s) if s else None
    aap = sea_ice_model.antarctic_min_projection(s) if s else None
    mp = methane_model.projection(ch4) if ch4 else None
    np_ = n2o_model.projection(n2o) if n2o else None
    enriched = markets_model.edges_for_markets(markets, gp, cp, ap, aap, mp, np_)
    return jsonify({
        "markets": enriched,
        "count": len(enriched),
        "gistemp_projection": gp,
        "co2_projection": cp,
        "methane_projection": mp,
        "n2o_projection": np_,
        "arctic_min_projection": ap,
        "antarctic_min_projection": aap,
        "temperature_thresholds": temperature_model.threshold_probs(gp),
        "co2_thresholds": co2_model.threshold_probs(cp),
        "methane_thresholds": methane_model.threshold_probs(mp),
        "n2o_thresholds": n2o_model.threshold_probs(np_),
    })


@app.route("/api/summary")
def api_summary():
    """Single endpoint giving the front page everything it needs in one shot."""
    g = gistemp_src.fetch()
    c = co2_src.fetch()
    s = sea_ice_src.fetch()
    o = oni_src.fetch()
    ch4 = methane_src.fetch()
    n2o = n2o_src.fetch()
    sf6 = sf6_src.fetch()
    gp = temperature_model.projection(g) if g else None
    cp = co2_model.projection(c) if c else None
    ap = sea_ice_model.arctic_min_projection(s) if s else None
    aap = sea_ice_model.antarctic_min_projection(s) if s else None
    mp = methane_model.projection(ch4) if ch4 else None
    np_ = n2o_model.projection(n2o) if n2o else None
    sp = sf6_model.projection(sf6) if sf6 else None
    forcing = forcing_model.compute(co2=c, methane=ch4, n2o=n2o, sf6=sf6)
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
        "sf6": {
            "latest": sf6["latest"] if sf6 else None,
            "projection": sp,
            "thresholds": sf6_model.threshold_probs(sp),
        },
        "forcing": forcing,
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


@app.route("/api/backtest")
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


# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting climate dashboard on :%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
