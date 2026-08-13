#!/usr/bin/env python3
"""Forecast Dashboard — FastAPI backend (port 7062).

Cross-venue (Polymarket + Kalshi) short-horizon forecast board with model
probabilities and calibration tracking.

Authentication: SSO via gateway-injected `X-Gateway-Secret` header, same
pattern as voters-dashboard. DEV_MODE=1 bypasses auth for local work.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import ask
import db
import ingest_kalshi
import ingest_manifold
import ingest_metaculus
import ingest_options
import ingest_polymarket
import matching
import models_calibrated
import models_fed
import models_llm
import models_weather
import news
import resolver

app = FastAPI(title="Forecast Dashboard")
log = logging.getLogger("forecast")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"

INGEST_INTERVAL = 600
MODELS_INTERVAL = 1800
RESOLVE_INTERVAL = 600
NEWS_INTERVAL = 1800

MODEL_MODULES = (models_weather, models_fed, models_llm, models_calibrated)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Auth (gateway SSO) ───────────────────────────────────────────────────────

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"

if not _sso_secret:
    if _DEV_MODE:
        log.warning("GATEWAY_SSO_SECRET not set — forecast dashboard running in DEV_MODE (no auth)")
    else:
        log.warning("GATEWAY_SSO_SECRET not set and DEV_MODE not enabled — rejecting all requests")


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    if request.url.path not in ("/healthz", "/api/signals"):
        if _sso_secret:
            client_secret = request.headers.get("x-gateway-secret", "")
            if not hmac.compare_digest(client_secret.encode(), _sso_secret.encode()):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        elif not _DEV_MODE:
            return JSONResponse({"error": "Service misconfigured"}, status_code=503)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'"
    )
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# ── Loop state ───────────────────────────────────────────────────────────────

_loop_state: dict[str, dict] = {
    "ingest": {"last_run": None, "last_ok": None, "last_error": None, "markets": 0},
    "models": {"last_run": None, "last_ok": None, "last_error": None, "rows": 0},
    "resolve": {"last_run": None, "last_ok": None, "last_error": None, "resolved": 0},
    "news": {"last_run": None, "last_ok": None, "last_error": None, "headlines": 0},
}
_bg_tasks: set = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _horizon_days() -> int:
    try:
        return int(os.environ.get("FORECAST_HORIZON_DAYS", "30"))
    except ValueError:
        return 30


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Kalshi's open 7-day universe exceeds 60k markets (hourly/per-game series);
# storing a snapshot for each every cycle would bloat the DB by millions of
# rows a day. Keep liquid/active markets plus the series the models and
# rulepacks consume regardless of size. The index prefixes are
# hyphen-terminated so the daily-range series match without dragging in the
# hourly micro variants (KXINXU, KXNASDAQ100U — hundreds of strikes a day).
_KALSHI_KEEP_SERIES = (
    "KXHIGH",
    "KXLOW",
    "KXFED",
    "KXINX-",
    "KXNASDAQ100-",
    "KXCPI",
    "KXPAYROLL",
    "KXU3",
    "KXGDP",
)
_KALSHI_MIN_LIQ = _env_float("FORECAST_KALSHI_MIN_LIQ", 1000)
_KALSHI_MIN_VOL24 = _env_float("FORECAST_KALSHI_MIN_VOL24", 500)


def _filter_kalshi(rows: list[dict]) -> list[dict]:
    kept = []
    for r in rows:
        series = (r.get("event_ticker") or r.get("venue_id") or "").upper()
        if series.startswith(_KALSHI_KEEP_SERIES) or (r.get("liquidity") or 0) >= _KALSHI_MIN_LIQ or (r.get("volume_24h") or 0) >= _KALSHI_MIN_VOL24:
            kept.append(r)
    if len(kept) < len(rows):
        log.info("kalshi filter: kept %d of %d rows (min_liq=%s, min_vol24=%s)", len(kept), len(rows), _KALSHI_MIN_LIQ, _KALSHI_MIN_VOL24)
    return kept


# ── Background loops (top-traders pattern) ───────────────────────────────────


def _ingest_store(market_dicts: list[dict]) -> int:
    conn = db.get_conn()
    try:
        for m in market_dicts:
            uid = db.upsert_market(conn, m)
            base = db.compute_baseline(m.get("yes_bid"), m.get("yes_ask"), m.get("last_price"), m.get("liquidity"))
            db.insert_snapshot(
                conn,
                {
                    "market_uid": uid,
                    "yes_bid": m.get("yes_bid"),
                    "yes_ask": m.get("yes_ask"),
                    "last_price": m.get("last_price"),
                    "baseline_prob": base["baseline"],
                    "spread": base["spread"],
                    "liquidity": m.get("liquidity"),
                    "volume_24h": m.get("volume_24h"),
                },
            )
        matching.match(conn)
        _log_daily_calibration(conn)
        return len(market_dicts)
    finally:
        conn.close()


def _log_daily_calibration(conn) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for row in db.latest_snapshots(conn, _horizon_days()):
        common = {
            "market_uid": row["uid"],
            "platform": row["venue"],
            "category": row["category"],
            "target_date": row["end_date"],
            "displayed_at": today,
            "market_prob": row["baseline_prob"],
        }
        db.log_calibration_row(
            conn,
            {
                **common,
                "source": "market_baseline",
                "model_prob": None,
                "prob_method": "market",
            },
        )
        for source, mp in (row.get("model_probs") or {}).items():
            db.log_calibration_row(
                conn,
                {
                    **common,
                    "source": source,
                    "model_prob": mp["model_prob"],
                    "prob_method": mp.get("prob_method") or "model",
                },
            )


async def _ingest_loop():
    await asyncio.sleep(5)
    while True:
        _loop_state["ingest"]["last_run"] = _now_iso()
        try:
            horizon = _horizon_days()
            results = await asyncio.gather(
                ingest_polymarket.fetch_horizon(horizon),
                ingest_kalshi.fetch_horizon(horizon),
                ingest_options.fetch_horizon(horizon),
                ingest_manifold.fetch_horizon(horizon),
                ingest_metaculus.fetch_horizon(horizon),
                return_exceptions=True,
            )
            rows: list[dict] = []
            for res, venue in zip(results, ("polymarket", "kalshi", "options", "manifold", "metaculus")):
                if isinstance(res, BaseException):
                    log.error("ingest %s failed: %s", venue, res)
                    continue
                res = res or []
                if venue == "kalshi":
                    res = _filter_kalshi(res)
                rows.extend(res)
            n = await asyncio.to_thread(_ingest_store, rows)
            _loop_state["ingest"].update(last_ok=_now_iso(), last_error=None, markets=n)
        except Exception as e:
            log.error("ingest loop error: %s", e)
            _loop_state["ingest"]["last_error"] = str(e)
        await asyncio.sleep(INGEST_INTERVAL)


def _models_store(rows: list[dict]) -> int:
    conn = db.get_conn()
    try:
        for row in rows:
            db.insert_model_prob(conn, row)
        return len(rows)
    finally:
        conn.close()


async def _models_loop():
    await asyncio.sleep(30)
    while True:
        _loop_state["models"]["last_run"] = _now_iso()
        try:
            markets = await asyncio.to_thread(_current_horizon_markets)
            rows: list[dict] = []
            for mod in MODEL_MODULES:
                try:
                    rows.extend(await mod.compute(markets) or [])
                except Exception as e:
                    log.error("model %s failed: %s", mod.__name__, e)
            n = await asyncio.to_thread(_models_store, rows)
            _loop_state["models"].update(last_ok=_now_iso(), last_error=None, rows=n)
        except Exception as e:
            log.error("models loop error: %s", e)
            _loop_state["models"]["last_error"] = str(e)
        await asyncio.sleep(MODELS_INTERVAL)


def _current_horizon_markets() -> list[dict]:
    conn = db.get_conn()
    try:
        return db.latest_snapshots(conn, _horizon_days())
    finally:
        conn.close()


async def _resolve_loop():
    await asyncio.sleep(60)
    while True:
        _loop_state["resolve"]["last_run"] = _now_iso()
        conn = None
        try:
            conn = await asyncio.to_thread(db.get_conn)
            n = await resolver.resolve_pending(conn)
            try:
                n += await resolver.adjudicate_custom(conn)
            except Exception as e:
                log.error("adjudicate_custom failed: %s", e)
            _loop_state["resolve"].update(last_ok=_now_iso(), last_error=None, resolved=n)
        except Exception as e:
            log.error("resolve loop error: %s", e)
            _loop_state["resolve"]["last_error"] = str(e)
        finally:
            if conn is not None:
                conn.close()
        await asyncio.sleep(RESOLVE_INTERVAL)


async def _news_loop():
    await asyncio.sleep(90)
    while True:
        _loop_state["news"]["last_run"] = _now_iso()
        conn = None
        try:
            conn = await asyncio.to_thread(db.get_conn)
            markets = await asyncio.to_thread(db.latest_snapshots, conn, _horizon_days())
            n = await news.refresh(conn, markets)
            _loop_state["news"].update(last_ok=_now_iso(), last_error=None, headlines=n)
        except Exception as e:
            log.error("news loop error: %s", e)
            _loop_state["news"]["last_error"] = str(e)
        finally:
            if conn is not None:
                conn.close()
        await asyncio.sleep(NEWS_INTERVAL)


@app.on_event("startup")
async def _start_loops():
    for coro in (_ingest_loop(), _models_loop(), _resolve_loop(), _news_loop()):
        task = asyncio.create_task(coro)
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


# ── Endpoints ────────────────────────────────────────────────────────────────


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/accuracy")
def accuracy_page():
    return FileResponse(STATIC_DIR / "accuracy.html")


def _latest_model(model_probs: dict) -> tuple[str | None, dict | None]:
    latest_source, latest = None, None
    for source, mp in (model_probs or {}).items():
        if latest is None or (mp.get("ts") or "") > (latest.get("ts") or ""):
            latest_source, latest = source, mp
    return latest_source, latest


_TIER_RANK = {"stale": 0, "thin": 1, "low": 2, "medium": 3, "high": 4}

# Recurring intraday series (5-min/hourly crypto up-or-down, per-game micro
# lines). They drown the board's actual signal, so they're opt-in.
_MICRO_RE = re.compile(
    r"\b(up or down|above .* at \d{1,2}:\d{2}\s*(AM|PM)|\d{1,2}:\d{2}\s*(AM|PM)\s*-\s*\d{1,2}:\d{2}\s*(AM|PM)"
    r"|map \d|total rounds|rounds handicap|games total|game handicap|\bo/u \d|over/under \d+\.5|1st half|first half|quarter \d)\b",
    re.IGNORECASE,
)


def _board_rows(days: int, category: str | None, venue: str | None, sort: str, min_confidence: str | None = None, include_micro: bool = False, q: str | None = None) -> list[dict]:
    conn = db.get_conn()
    try:
        rows = db.latest_snapshots(conn, days)
        if not include_micro:
            rows = [r for r in rows if not _MICRO_RE.search(r.get("question") or "")]
        if category:
            rows = [r for r in rows if (r.get("category") or "").lower() == category.lower()]
        if venue:
            rows = [r for r in rows if (r.get("venue") or "").lower() == venue.lower()]
        if q:
            terms = [t.lower() for t in db.search_terms(q)]
            # A query that parses to zero terms (punctuation-only) matches
            # nothing rather than silently returning the whole board.
            rows = [r for r in rows if terms and all(t in (r.get("question") or "").lower() for t in terms)]

        link_map: dict[str, str] = {}
        for lk in db.links(conn):
            if lk["status"] in ("confirmed", "auto"):
                link_map[lk["poly_uid"]] = lk["kalshi_uid"]
                link_map[lk["kalshi_uid"]] = lk["poly_uid"]

        baseline_by_uid = {r["uid"]: r["baseline_prob"] for r in rows}
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

        out = []
        for r in rows:
            model_source, model = _latest_model(r.get("model_probs") or {})
            model_prob = model["model_prob"] if model else None
            display_prob = model_prob if model_prob is not None else r["baseline_prob"]

            prev = db.baseline_asof(conn, r["uid"], cutoff_24h)
            move_24h = None
            is_new = prev is None
            if prev is not None and r["baseline_prob"] is not None:
                move_24h = round(r["baseline_prob"] - prev, 4)

            tier = db.compute_baseline(r.get("yes_bid"), r.get("yes_ask"), r.get("last_price"), r.get("liquidity"))["tier"]
            if min_confidence and _TIER_RANK.get(tier, 0) < _TIER_RANK.get(min_confidence.lower(), 0):
                continue

            disagreement = None
            counterpart = link_map.get(r["uid"])
            if counterpart is not None:
                other = baseline_by_uid.get(counterpart)
                if other is None:
                    snap = db.latest_snapshot(conn, counterpart)
                    other = snap["baseline_prob"] if snap else None
                if other is not None and r["baseline_prob"] is not None:
                    cp_market = db.get_market(conn, counterpart) or {}
                    disagreement = {
                        "counterpart_uid": counterpart,
                        "counterpart_venue": cp_market.get("venue"),
                        "counterpart_url": cp_market.get("url"),
                        "delta": round(r["baseline_prob"] - other, 4),
                    }

            out.append(
                {
                    "uid": r["uid"],
                    "venue": r["venue"],
                    "question": r["question"],
                    "category": r["category"],
                    "url": r["url"],
                    "end_date": r["end_date"],
                    "display_prob": display_prob,
                    "baseline_prob": r["baseline_prob"],
                    "model_source": model_source,
                    "model_prob": model_prob,
                    "move_24h": move_24h,
                    "new": is_new,
                    "spread": r["spread"],
                    "confidence": tier,
                    "liquidity": r["liquidity"],
                    "volume_24h": r["volume_24h"],
                    "disagreement": disagreement,
                    "resolved": r["resolved"],
                    "outcome": r["outcome"],
                    "resolved_at": r["resolved_at"],
                    "snapshot_ts": r["snapshot_ts"],
                }
            )

        if sort == "move_24h":
            out.sort(key=lambda x: abs(x["move_24h"] or 0), reverse=True)
        elif sort == "liquidity":
            out.sort(key=lambda x: x["liquidity"] or 0, reverse=True)
        elif sort == "spread":
            out.sort(key=lambda x: x["spread"] if x["spread"] is not None else 1.0)
        else:
            out.sort(key=lambda x: ((x["end_date"] or "9999")[:10], -_TIER_RANK.get(x["confidence"] or "stale", 0), -(x["liquidity"] or 0), x["end_date"] or "9999"))
        return out
    finally:
        conn.close()


@app.get("/api/board")
def api_board(
    days: int = Query(7, ge=1, le=90),
    category: str = Query(""),
    venue: str = Query(""),
    sort: str = Query("end_date"),
    min_confidence: str = Query(""),
    include_micro: bool = Query(False),
    q: str = Query(""),
):
    if min_confidence and min_confidence.lower() not in _TIER_RANK:
        raise HTTPException(status_code=400, detail=f"min_confidence must be one of {sorted(_TIER_RANK)}")
    return _board_rows(days, category or None, venue or None, sort, min_confidence or None, include_micro, q.strip() or None)


@app.get("/api/ask")
async def api_ask(q: str = Query("")):
    question = q.strip()
    if not 5 <= len(question) <= 300:
        raise HTTPException(status_code=400, detail="q must be between 5 and 300 characters")
    return await ask.ask(question)


@app.get("/api/event/{uid}")
def api_event(uid: str):
    conn = db.get_conn()
    try:
        market = db.get_market(conn, uid)
        if market is None:
            raise HTTPException(status_code=404, detail="unknown market uid")
        return {
            "market": market,
            "snapshots": db.snapshot_history(conn, uid),
            "model_probs": db.model_prob_history(conn, uid),
            "news": db.news_for(conn, uid),
        }
    finally:
        conn.close()


@app.get("/api/disagreements")
def api_disagreements():
    conn = db.get_conn()
    try:
        return matching.disagreements(conn)
    finally:
        conn.close()


_RESERVED_SOURCES = frozenset({"market_baseline", "calibrated", "llm", "weather", "fed_implied", "weather_model", "fed_model", "market"})
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{2,39}$")


@app.post("/api/signals")
async def api_signals(request: Request):
    """External predictors (stock models, social-signal tools, ...) push
    probabilities here. Every signal is logged into the calibration ledger
    and scored against outcomes — the scorecard settles whether the source
    has edge. Auth: Bearer FORECAST_SIGNALS_TOKEN (machine-to-machine)."""
    token = os.environ.get("FORECAST_SIGNALS_TOKEN", "")
    if not token:
        return JSONResponse({"error": "signals API disabled — set FORECAST_SIGNALS_TOKEN"}, status_code=503)
    header = request.headers.get("authorization") or ""
    if not header.startswith("Bearer "):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Bytes compare: hmac.compare_digest raises on non-ASCII str input, which
    # would turn a garbage header into a 500 instead of a 401.
    if not hmac.compare_digest(header[len("Bearer "):].encode(), token.encode()):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    source = str(body.get("source") or "").lower()
    if not _SOURCE_RE.match(source) or source in _RESERVED_SOURCES:
        raise HTTPException(status_code=400, detail="source must match ^[a-z0-9][a-z0-9_-]{2,39}$ and not be a reserved name")
    try:
        prob = float(body.get("probability"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="probability must be a number")
    if not 0.0 < prob < 1.0:
        raise HTTPException(status_code=400, detail="probability must be strictly between 0 and 1")

    conn = db.get_conn()
    try:
        uid = body.get("market_uid")
        if uid:
            market = db.get_market(conn, uid)
            if market is None:
                raise HTTPException(status_code=404, detail="unknown market_uid")
        else:
            question = (body.get("question") or "").strip()
            if not 5 <= len(question) <= 300:
                raise HTTPException(status_code=400, detail="provide market_uid, or a question of 5-300 chars")
            uid = db.create_custom_market(conn, question, body.get("end_date"))
            market = db.get_market(conn, uid)

        meta = body.get("meta")
        if meta is not None and len(json.dumps(meta)) > 4096:
            raise HTTPException(status_code=413, detail="meta too large (4KB max, serialized)")
        db.insert_model_prob(
            conn,
            {
                "market_uid": uid,
                "source": source,
                "model_prob": round(prob, 4),
                "prob_method": "external",
                "detail": json.dumps({"meta": meta} if meta is not None else {}),
            },
        )
        snap = db.latest_snapshot(conn, uid)
        db.log_calibration_row(
            conn,
            {
                "market_uid": uid,
                "platform": market.get("venue"),
                "category": market.get("category"),
                "target_date": market.get("end_date"),
                "source": source,
                "model_prob": round(prob, 4),
                "market_prob": snap.get("baseline_prob") if snap else None,
                "displayed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "prob_method": "external",
            },
        )
        scored = bool(snap and snap.get("baseline_prob") is not None)
        return {"status": "logged", "uid": uid, "source": source, "probability": round(prob, 4), "scored_against_market": scored}
    finally:
        conn.close()


def _verdict(row: dict) -> str:
    if row["n"] < 30 or row["se"] is None:
        return "insufficient_data"
    if row["edge"] > 2 * row["se"]:
        return "beats_market"
    if row["edge"] < -2 * row["se"]:
        return "below_market"
    return "no_edge_yet"


@app.get("/api/scorecard")
def api_scorecard():
    conn = db.get_conn()
    try:
        rows = db.source_scorecard(conn)
    finally:
        conn.close()
    for r in rows:
        r["verdict"] = _verdict(r)
    return {"sources": rows, "verdict_rule": "beats_market / below_market require |edge| > 2 SE over >= 30 paired resolutions; anything else is unproven"}


@app.get("/api/accuracy")
def api_accuracy():
    conn = db.get_conn()
    try:
        return db.get_brier_score(conn)
    finally:
        conn.close()


@app.get("/api/status")
def api_status():
    conn = db.get_conn()
    try:
        return {
            "dev_mode": _DEV_MODE,
            "horizon_days": _horizon_days(),
            "loops": _loop_state,
            "counts": db.status_counts(conn),
        }
    finally:
        conn.close()


@app.post("/api/links")
def api_links(payload: dict):
    poly_uid = payload.get("poly_uid")
    kalshi_uid = payload.get("kalshi_uid")
    action = payload.get("action")
    if not poly_uid or not kalshi_uid:
        raise HTTPException(status_code=400, detail="poly_uid and kalshi_uid required")
    if action not in ("confirm", "reject"):
        raise HTTPException(status_code=400, detail="action must be 'confirm' or 'reject'")
    status = "confirmed" if action == "confirm" else "rejected"
    conn = db.get_conn()
    try:
        if not db.set_link_status(conn, poly_uid, kalshi_uid, status):
            raise HTTPException(status_code=404, detail="link not found")
        return {"poly_uid": poly_uid, "kalshi_uid": kalshi_uid, "status": status}
    finally:
        conn.close()
