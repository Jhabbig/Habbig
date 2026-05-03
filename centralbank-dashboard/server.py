#!/usr/bin/env python3
"""Central Bank Dashboard — FastAPI backend.

v0 surface:
  - GET /          → index.html (rate-path chart)
  - GET /api/rates → cached FRED policy rates (JSON)

Auth: same gateway-SSO pattern as world-state-dashboard. Set DEV_MODE=1 to
bypass when running locally.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from analysis import edge as edge_analysis
from analysis import stance as stance_analysis
from ingestion import decision_calendar, econ_releases, fred_client, implied_path, kalshi_client, ois_curve
from trading import audit as trade_audit
from trading import key_store
from trading import order_manager

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Central Bank Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"

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


@app.get("/api/rates")
async def api_rates(force: bool = False) -> JSONResponse:
    return JSONResponse(fred_client.get_cached_rates(force=force))


@app.get("/api/calendar")
async def api_calendar(horizon_days: int = 90) -> JSONResponse:
    horizon_days = max(1, min(horizon_days, 365))
    return JSONResponse(decision_calendar.get_calendar(horizon_days=horizon_days))


@app.get("/api/implied")
async def api_implied(force: bool = False) -> JSONResponse:
    return JSONResponse(implied_path.get_cached(force=force))


@app.get("/api/ois")
async def api_ois(months_ahead: int = 18, force: bool = False) -> JSONResponse:
    months_ahead = max(3, min(months_ahead, 36))
    return JSONResponse(ois_curve.get_cached(months_ahead=months_ahead, force=force))


@app.get("/api/econ")
async def api_econ(force: bool = False) -> JSONResponse:
    return JSONResponse(econ_releases.get_cached(force=force))


@app.get("/api/edge")
async def api_edge() -> JSONResponse:
    return JSONResponse(edge_analysis.compute())


@app.get("/api/kalshi")
async def api_kalshi(force: bool = False) -> JSONResponse:
    """Raw Kalshi FOMC markets — useful for debugging the cross-venue join."""
    from datetime import date as _date, datetime as _dt, timezone as _tz
    today = _dt.now(_tz.utc).date()
    cal = decision_calendar.upcoming(today, horizon_days=120)
    fomc = next((m for m in cal if m["cb"] == "US"), None)
    if not fomc:
        return JSONResponse({"meeting": None, "markets": []})
    md = _date.fromisoformat(fomc["decision_date"])
    rates = fred_client.get_cached_rates()
    dff = next((s for s in rates["series"] if s["series_id"] == "DFF"), None)
    rate = dff["latest"][1] if dff and dff["latest"] else None
    return JSONResponse({
        "meeting": fomc,
        "current_rate": rate,
        "markets": kalshi_client.get_cached_for_meeting(md, rate, force=force),
    })


@app.get("/api/stance")
async def api_stance() -> JSONResponse:
    return JSONResponse(stance_analysis.compute())


# ── Trading endpoints (Phase 2) ──────────────────────────────────────────────
#
# Identity: every trading endpoint requires an authenticated user. We trust
# the gateway's `X-Gateway-User-Id` header — it's set by gateway/server.py
# after the SSO check passes. In DEV_MODE we fall back to a single "dev"
# user_id so a developer can exercise the trading flow on localhost without
# running the full gateway.
#
# Safety: every order placement requires `confirm: true` in the request
# body. The frontend only sets that after the user clicks "Confirm" in a
# modal that re-states the order details. We never auto-trade based on
# any signal.

def _require_user_id(request: Request) -> str | JSONResponse:
    uid = request.headers.get("x-gateway-user-id", "").strip()
    if not uid:
        if _DEV_MODE:
            return "dev-user"
        return JSONResponse(
            {"error": "trading endpoints require gateway user identity"},
            status_code=401,
        )
    return uid


class KalshiKeyPayload(BaseModel):
    api_key_id: str = Field(min_length=4, max_length=200)
    private_key_pem: str = Field(min_length=100, max_length=10000)
    mode: str = Field(default="paper", pattern="^(paper|prod)$")


class ModePayload(BaseModel):
    mode: str = Field(pattern="^(paper|prod)$")
    confirm_real_money: bool = False  # required for paper → prod


class OrderPayload(BaseModel):
    ticker: str = Field(min_length=4, max_length=120)
    side: str = Field(pattern="^(yes|no)$")
    action: str = Field(pattern="^(buy|sell)$")
    count: int = Field(ge=1, le=10000)
    price_cents: int = Field(ge=1, le=99)
    confirm: bool = False
    client_order_id: str | None = Field(default=None, max_length=80)


@app.get("/api/keys/kalshi/status")
async def api_key_status(request: Request) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    return JSONResponse(key_store.status(uid))


@app.post("/api/keys/kalshi")
async def api_keys_upsert(request: Request, body: KalshiKeyPayload) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    try:
        key_store.upsert_key(
            user_id=uid,
            api_key_id=body.api_key_id,
            private_key_pem=body.private_key_pem,
            mode=body.mode,
        )
    except (ValueError, RuntimeError) as exc:
        trade_audit.write_event(uid, "key.upsert", ok=False, error=str(exc), mode=body.mode)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    trade_audit.write_event(uid, "key.upsert", ok=True, mode=body.mode)
    return JSONResponse({"ok": True, "mode": body.mode})


@app.delete("/api/keys/kalshi")
async def api_keys_delete(request: Request) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    removed = key_store.delete_key(uid)
    trade_audit.write_event(uid, "key.delete", ok=removed)
    return JSONResponse({"ok": True, "removed": removed})


@app.post("/api/keys/kalshi/mode")
async def api_keys_set_mode(request: Request, body: ModePayload) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    if body.mode == "prod" and not body.confirm_real_money:
        return JSONResponse(
            {"ok": False, "error": "switching to prod requires confirm_real_money=true"},
            status_code=400,
        )
    ok = key_store.set_mode(uid, body.mode)
    trade_audit.write_event(uid, "mode.set", ok=ok, mode=body.mode)
    return JSONResponse({"ok": ok, "mode": body.mode})


@app.get("/api/portfolio/balance")
async def api_balance(request: Request) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    return JSONResponse(order_manager.get_balance(uid))


@app.get("/api/portfolio/positions")
async def api_positions(request: Request) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    return JSONResponse(order_manager.get_positions(uid))


@app.get("/api/orders")
async def api_orders(request: Request) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    return JSONResponse(order_manager.list_orders(uid))


@app.post("/api/order/kalshi")
async def api_order_place(request: Request, body: OrderPayload) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    if not body.confirm:
        return JSONResponse(
            {"ok": False, "error": "order requires explicit confirm=true (user must accept the modal)"},
            status_code=400,
        )
    result = order_manager.place_order(
        uid,
        ticker=body.ticker,
        side=body.side,
        action=body.action,
        count=body.count,
        price_cents=body.price_cents,
        client_order_id=body.client_order_id,
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.delete("/api/order/kalshi/{order_id}")
async def api_order_cancel(request: Request, order_id: str) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    result = order_manager.cancel_order(uid, order_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/api/audit")
async def api_audit(request: Request, limit: int = 100) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    limit = max(1, min(limit, 500))
    return JSONResponse({"events": trade_audit.tail_for_user(uid, limit=limit)})


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "7060")))
