"""Intelligence + credibility + backtests + retrospective + environmental.

These routes all sit downstream of the narve intelligence layer —
credibility scores, backtest jobs, retrospective analyses, Claude-generated
environmental impact summaries. Extracted from server.py with no behaviour
change; all shared-state access goes through ``_srv()``.

The ``POLY_CLIENT``/``KALSHI_CLIENT`` singletons and ``MARKETS_CACHE_TTL``
still live in server.py (shutdown handler + switcher depend on them) —
this module fetches them lazily so the module-import order stays safe.
"""

from __future__ import annotations

import json as _json
import logging
import sys
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import db
from backend.markets import unified_markets
from intelligence import environmental as _env_module


log = logging.getLogger("gateway.intelligence_routes")


def _srv():
    """Return the already-imported server module (for helpers + globals)."""
    return sys.modules.get("server") or sys.modules["__main__"]


def _serialize_env_payload(payload: dict, unit: str = "co2_mt") -> dict:
    """Render an env payload for API output, applying the user's unit preference."""
    return _env_module.apply_user_unit_preference(payload, unit)


# ── Credibility ────────────────────────────────────────────────────────────


async def api_get_credibility(request: Request, source_handle: str):
    _srv()._require_authenticated(request)
    cred = db.get_source_credibility(source_handle)
    if not cred:
        return JSONResponse({"source_handle": source_handle, "global_credibility": None, "status": "unknown"})
    cats = db.get_all_category_credibilities(source_handle)
    snaps = db.get_credibility_snapshots(source_handle, 5)
    return JSONResponse({
        "source_handle": source_handle,
        "global_credibility": cred["global_credibility"],
        "accuracy_unlocked": bool(cred["accuracy_unlocked"]),
        "decay_weighted_accuracy": cred["decay_weighted_accuracy"],
        "total_predictions": cred["total_predictions"],
        "correct_predictions": cred["correct_predictions"],
        "categories": [
            {"category": c["category"], "credibility": c["category_credibility"],
             "prediction_count": c["prediction_count"]}
            for c in cats
        ],
        "snapshots": [{"credibility": s["global_credibility"], "at": s["snapshot_at"]} for s in snaps],
    })


async def api_get_calibration(request: Request, source_handle: str):
    """Calibration data for a source (F9).

    Returns the calibration score and per-bucket data showing how well
    the source's stated probabilities match actual outcomes.
    """
    _srv()._require_authenticated(request)
    cal = db.get_source_calibration(source_handle)
    if not cal:
        return JSONResponse({
            "source_handle": source_handle,
            "calibration": None,
            "status": "insufficient_data",
        })
    return JSONResponse({
        "source_handle": source_handle,
        "calibration": cal,
    })


async def api_credibility_refresh(request: Request):
    srv = _srv()
    user = srv._require_pro_user(request)
    # Force-refresh recomputes EVERY source's credibility — expensive. Cap
    # at 2 per 5 minutes per user so a single Pro user cannot DoS the engine.
    if srv._is_rate_limited(f"cred_refresh:{user['user_id']}", limit=2, window=300):
        return JSONResponse(
            {"error": "Credibility refresh available once every 5 minutes."},
            status_code=429,
            headers={"Retry-After": "300"},
        )
    count = db.recompute_all_credibilities()
    log.info("User %s triggered credibility refresh, recomputed %d sources", user.get("username"), count)
    return JSONResponse({"recomputed": count, "timestamp": int(time.time())})


# ── Backtests ──────────────────────────────────────────────────────────────


async def api_create_backtest(request: Request):
    """Submit a backtest job. Returns backtest_id to poll for results."""
    user = _srv()._require_pro_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    params = {
        "min_credibility": float(body.get("min_credibility", 0.5)),
        "min_edge": float(body.get("min_edge", 0.05)),
        "category": body.get("category") or None,
        "bet_sizing": body.get("bet_sizing", "flat"),
        "bankroll": float(body.get("bankroll", 10000)),
        "max_bet_pct": float(body.get("max_bet_pct", 0.1)),
    }

    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO backtests (user_id, params, status, created_at) VALUES (?, ?, 'pending', ?)",
            (user["user_id"], _json.dumps(params), now),
        )
        backtest_id = cur.lastrowid

    # Run as a background job
    from jobs import enqueue_job
    await enqueue_job("run_backtest", backtest_id=backtest_id)

    return JSONResponse({"backtest_id": backtest_id, "status": "pending"})


async def api_get_backtest(request: Request, backtest_id: int):
    """Get backtest results. Poll until status=completed."""
    user = _srv()._require_pro_user(request)
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM backtests WHERE id = ? AND user_id = ?",
            (backtest_id, user["user_id"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Backtest not found")
    result = _json.loads(row["result"]) if row["result"] else None
    return JSONResponse({
        "backtest_id": row["id"],
        "status": row["status"],
        "params": _json.loads(row["params"]),
        "result": result,
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    })


# ── Retrospective ──────────────────────────────────────────────────────────


async def api_market_retrospective(request: Request, market_id: str):
    """Get the post-resolution retrospective for a resolved market.

    Returns the Claude-generated analysis of how narve.ai's intelligence
    performed, including which sources called it correctly and which were wrong.
    """
    _srv()._require_authenticated(request)
    from intelligence.retrospective import _get_cached
    retro = _get_cached(market_id)
    if not retro:
        return JSONResponse({"retrospective": None, "market_id": market_id})
    return JSONResponse({"retrospective": retro, "market_id": market_id})


# ── Probability ────────────────────────────────────────────────────────────


async def api_market_probability(request: Request, market_id: str):
    srv = _srv()
    srv._require_authenticated(request)
    predictions = db.get_predictions_for_market(market_id)
    pred_dicts = [
        {
            "source_handle": p["source_handle"],
            "direction": p["direction"],
            "predicted_probability": p["predicted_probability"],
            "global_credibility": p["global_credibility"],
            "category_credibility": p["category_credibility"] if "category_credibility" in p.keys() else None,
            "accuracy_unlocked": bool(p["accuracy_unlocked"]) if p["accuracy_unlocked"] is not None else False,
        }
        for p in predictions
    ]
    result = db.calculate_betyc_probability(pred_dicts)
    market = await unified_markets.fetch_single_market(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    market_yes = market.yes_price if market else None
    if market_yes is not None and result["betyc_yes_probability"] is not None:
        result["betyc_edge"] = round(result["betyc_yes_probability"] - market_yes, 4)
    result["market_yes_price"] = market_yes
    result["contributing_sources"] = [
        {"handle": p["source_handle"], "credibility": p.get("global_credibility"),
         "predicted_probability": p.get("predicted_probability"),
         "category_credibility": p.get("category_credibility")}
        for p in pred_dicts
    ]
    return JSONResponse(result)


# ── Environmental Impact (Pro feature) ────────────────────────────────────


async def api_environmental_top(request: Request, limit: int = 20):
    user = _srv()._require_pro_user(request)
    limit = max(1, min(50, int(limit)))
    rows = db.list_top_environmental_impacts(limit=limit)
    prefs = db.get_user_env_preferences(user["user_id"])
    unit = prefs.get("unit", "co2_mt")
    impacts = []
    for row in rows:
        payload = _env_module._row_to_payload(row)
        impacts.append(_serialize_env_payload(payload, unit))
    return JSONResponse({"impacts": impacts, "as_of": int(time.time()), "count": len(impacts)})


async def api_market_environmental(request: Request, market_id: str):
    srv = _srv()
    user = srv._require_pro_user(request)
    market = await unified_markets.fetch_single_market(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    payload = await _env_module.generate_environmental_impact(market, force=False)
    prefs = db.get_user_env_preferences(user["user_id"])
    return JSONResponse(_serialize_env_payload(payload, prefs.get("unit", "co2_mt")))


async def api_market_environmental_refresh(request: Request, market_id: str):
    srv = _srv()
    user = srv._require_pro_user(request)
    # Per-user rate limit: 5 force-refreshes per 24h. Stops a curious user
    # from running up the Claude bill exploring the same market repeatedly.
    if srv._is_rate_limited(f"env_refresh:{user['user_id']}", 5, 86400):
        return JSONResponse(
            {"error": "Force-refresh limit reached (5 per day). The cached analysis is still available via GET."},
            status_code=429,
            headers={"Retry-After": "86400"},
        )
    market = await unified_markets.fetch_single_market(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    payload = await _env_module.generate_environmental_impact(market, force=True)
    prefs = db.get_user_env_preferences(user["user_id"])
    log.info("Pro user %s force-refreshed env analysis for %s", user.get("email"), market_id)
    return JSONResponse(_serialize_env_payload(payload, prefs.get("unit", "co2_mt")))


async def api_user_env_preferences(request: Request):
    user = _srv()._require_authenticated(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    show = bool(body.get("show_environmental_impact", True))
    unit = (body.get("preferred_unit") or "co2_mt").strip().lower()
    if unit not in db.ENV_VALID_UNITS:
        return JSONResponse(
            {"error": f"preferred_unit must be one of {sorted(db.ENV_VALID_UNITS)}"},
            status_code=400,
        )
    try:
        db.set_user_env_preferences(user["user_id"], show=show, unit=unit)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({
        "show_environmental_impact": show,
        "preferred_unit": unit,
    })


def register(app) -> None:
    """Wire credibility + backtests + retrospective + probability + environmental
    routes into the given FastAPI app.

    Route ORDER matters: /api/markets/environmental/top must be registered
    BEFORE /api/markets/{market_id:path}/environmental so FastAPI doesn't
    consume "environmental/top" as a market_id.
    """
    app.add_api_route("/api/credibility/{source_handle}", api_get_credibility, methods=["GET"])
    app.add_api_route("/api/credibility/{source_handle}/calibration", api_get_calibration, methods=["GET"])
    app.add_api_route("/api/credibility/refresh", api_credibility_refresh, methods=["POST"])
    app.add_api_route("/api/backtests", api_create_backtest, methods=["POST"])
    app.add_api_route("/api/backtests/{backtest_id}", api_get_backtest, methods=["GET"])
    app.add_api_route("/api/markets/{market_id:path}/retrospective", api_market_retrospective, methods=["GET"])
    app.add_api_route("/api/markets/{market_id:path}/probability", api_market_probability, methods=["GET"])
    app.add_api_route("/api/markets/environmental/top", api_environmental_top, methods=["GET"])
    app.add_api_route("/api/markets/{market_id:path}/environmental", api_market_environmental, methods=["GET"])
    app.add_api_route("/api/markets/{market_id:path}/environmental/refresh", api_market_environmental_refresh, methods=["POST"])
    app.add_api_route("/api/user/preferences/environmental", api_user_env_preferences, methods=["PATCH"])
