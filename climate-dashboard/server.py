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

import hmac
import logging
import os
import subprocess
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory

from app.fetchers import co2 as co2_src
from app.fetchers import gistemp as gistemp_src
from app.fetchers import methane as methane_src
from app.fetchers import n2o as n2o_src
from app.fetchers import oni as oni_src
from app.fetchers import polymarket as polymarket_src
from app.fetchers import sea_ice as sea_ice_src
from app.fetchers import sst as sst_src
from app.methodology import payload as methodology_payload
from app.models import co2 as co2_model
from app.models import calibration
from app.models import markets as markets_model
from app.models import methane as methane_model
from app.models import n2o as n2o_model
from app.models import sea_ice as sea_ice_model
from app.models import temperature as temperature_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("climate")

app = Flask(__name__, static_folder="static")

try:
    from flask_compress import Compress

    Compress(app)
except Exception:
    logger.warning("flask_compress not available; responses will not be gzipped")


# ─── Gateway SSO check ─────────────────────────────────────────────────────────
#
# Climate-dashboard binds 0.0.0.0 (so the gateway / docker bridge can reach it).
# That means anyone on Tailscale or the local LAN could hit our port directly,
# bypassing the gateway's auth. Mirror the pattern used by voters/world-state/
# world-health: every request must carry a matching `x-gateway-secret` header
# (set by the gateway after its own login + subscription check).
#
# In DEV_MODE we skip the check so a developer can curl localhost without a
# gateway. In production with no secret set, fail closed (503) — never silently
# open.

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret:
    if _DEV_MODE:
        logger.warning("GATEWAY_SSO_SECRET unset — climate dashboard running in DEV_MODE (no auth)")
    else:
        logger.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — all non-healthz requests will 503")


@app.before_request
def _gateway_sso_check():
    # Health checks must always succeed for systemd / docker readiness probes.
    if request.path in ("/healthz", "/api/health"):
        return None
    if _sso_secret:
        client_secret = request.headers.get("x-gateway-secret", "")
        if not hmac.compare_digest(client_secret, _sso_secret):
            return jsonify({"error": "Unauthorized"}), 401
    elif not _DEV_MODE:
        return jsonify({"error": "Service misconfigured"}), 503
    return None


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


@app.route("/cesium")
def cesium():
    """3D globe view with NASA GIBS satellite imagery overlays."""
    return send_from_directory("static", "cesium.html")


@app.route("/methodology")
def methodology_page():
    return send_from_directory("static", "methodology.html")


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "climate-dashboard",
                    "commit": _COMMIT, "ts": time.time()})


@app.route("/healthz")
def healthz():
    """Uniform liveness endpoint shared with the other dashboards."""
    return jsonify({"ok": True})


@app.route("/api/methodology")
def api_methodology():
    return jsonify(methodology_payload(commit=_COMMIT))


# ─── Per-source endpoints ──────────────────────────────────────────────────────

@app.route("/api/temperature")
def api_temperature():
    g = gistemp_src.fetch()
    if not g:
        return jsonify({"error": "GISTEMP fetch failed"}), 503
    return jsonify({**g, "projection": temperature_model.projection(g)})


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


@app.route("/api/summary")
def api_summary():
    """Single endpoint giving the front page everything it needs in one shot."""
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
    app.run(host=os.environ.get("BIND_HOST", "0.0.0.0"), port=PORT, debug=False, threaded=True)
