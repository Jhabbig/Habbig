#!/usr/bin/env python3
"""Central Bank Dashboard — FastAPI backend.

v0 surface:
  - GET /          → index.html (rate-path chart)
  - GET /api/rates → cached FRED policy rates (JSON)

Auth: same gateway-SSO pattern as world-state-dashboard. Set DEV_MODE=1 to
bypass when running locally.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
from pathlib import Path

from fastapi import FastAPI, Path as FPath, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from analysis import edge as edge_analysis
from analysis import historical_store
from analysis import right_now as right_now_analysis
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
_BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
_BEHIND_TLS = os.environ.get("BEHIND_TLS", "").strip() == "1"

# C1: Refuse the shared "dev-user" fallback when bound to a non-loopback
# interface — that combination is a multi-tenant footgun (every visitor would
# share one Kalshi key). Loopback bindings are fine for laptop dev.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_DEV_MODE_LOOPBACK = _BIND_HOST in _LOOPBACK_HOSTS

if _DEV_MODE and not _DEV_MODE_LOOPBACK:
    log.critical(
        "DEV_MODE=1 with non-loopback BIND_HOST=%s — shared-user fallback DISABLED. Trading endpoints will require X-Gateway-User-Id even in dev.",
        _BIND_HOST,
    )

if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — all requests will 503")


# M2: hash the inline <script> in index.html so we can drop 'unsafe-inline'
# from script-src. We still keep 'unsafe-inline' for styles (the dashboard
# uses inline style="" attributes in many places — switching to nonces would
# require a full HTML refactor).
def _compute_inline_script_hash() -> str | None:
    try:
        html = HTML_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    # Match the first <script> block (no src attribute) and hash its body.
    m = re.search(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.DOTALL)
    if not m:
        return None
    body = m.group(1).encode("utf-8")
    digest = hashlib.sha256(body).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


_INLINE_SCRIPT_HASH = _compute_inline_script_hash()


# C5: trading routes that mutate state must require an explicit XHR header
# so a cross-site GET-from-a-form attempt cannot trigger them. We pair this
# with cookie-bound auth at the gateway layer.
_CSRF_PROTECTED_PREFIXES = ("/api/keys", "/api/order")
_CSRF_PROTECTED_METHODS = {"POST", "DELETE", "PUT", "PATCH"}


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    if request.url.path != "/healthz":
        if _sso_secret:
            client_secret = request.headers.get("x-gateway-secret", "")
            if not hmac.compare_digest(client_secret, _sso_secret):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        elif not _DEV_MODE:
            return JSONResponse({"error": "Service misconfigured"}, status_code=503)

        # C5: CSRF gate for state-changing trading routes.
        if request.method in _CSRF_PROTECTED_METHODS and any(request.url.path.startswith(p) for p in _CSRF_PROTECTED_PREFIXES):
            xrw = request.headers.get("x-requested-with", "")
            if xrw != "XMLHttpRequest":
                return JSONResponse(
                    {"error": "missing X-Requested-With: XMLHttpRequest header"},
                    status_code=403,
                )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    script_src = "'self'"
    if _INLINE_SCRIPT_HASH:
        script_src = f"'self' {_INLINE_SCRIPT_HASH}"
    else:
        # Fallback if hash computation failed; logged at startup.
        script_src = "'self' 'unsafe-inline'"
    response.headers["Content-Security-Policy"] = f"default-src 'self'; script-src {script_src}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    # M35: HSTS belongs to transport, not auth. Gate on BEHIND_TLS so a
    # TLS-terminating proxy in dev (e.g. Tailscale Funnel) still gets it.
    if _BEHIND_TLS or _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/api/right-now")
async def api_right_now(force: bool = False) -> JSONResponse:
    """Synthesized one-paragraph dashboard summary. Cached 60s — composed
    from the existing analysis caches without refetching anything."""
    data = await asyncio.to_thread(right_now_analysis.get_cached, force=force)
    return JSONResponse(data)


# Conservative key shape — match what `historical_store` writes; bound length
# and reject anything outside [a-zA-Z0-9._-] so a malicious caller can't smuggle
# SQL or path traversal into a series_key parameter.
_HISTORY_KEY_RX = re.compile(r"^[a-zA-Z0-9_.\-]{1,80}$")


@app.get("/api/history/{series_key}")
async def api_history_series(series_key: str) -> JSONResponse:
    """All historical samples + standard delta windows for a single series."""
    if not _HISTORY_KEY_RX.match(series_key):
        return JSONResponse({"error": "invalid series_key"}, status_code=400)
    summary = await asyncio.to_thread(historical_store.delta_summary, series_key)
    return JSONResponse(summary)


@app.get("/api/history")
async def api_history_summary(keys: str | None = None) -> JSONResponse:
    """Bulk delta summary — pass ``?keys=poly_price.cut25,kalshi_price.cut25``
    to get one summary per key. Without ``keys`` returns a stats blob so the
    operator can see how much history is currently stored."""
    if not keys:
        stats = await asyncio.to_thread(historical_store.stats)
        return JSONResponse({"stats": stats})
    requested = [k.strip() for k in keys.split(",") if k.strip()]
    # Reject anything that doesn't pass the key-shape check — a single bad
    # entry shouldn't poison the whole batch.
    safe = [k for k in requested if _HISTORY_KEY_RX.match(k)]
    summaries = await asyncio.to_thread(
        lambda: {k: historical_store.delta_summary(k) for k in safe},
    )
    return JSONResponse(
        {
            "requested": requested,
            "rejected": [k for k in requested if k not in safe],
            "summaries": summaries,
        }
    )


@app.get("/api/rates")
async def api_rates(force: bool = False) -> JSONResponse:
    data = await asyncio.to_thread(fred_client.get_cached_rates, force=force)
    return JSONResponse(data)


@app.get("/api/calendar")
async def api_calendar(horizon_days: int = 90) -> JSONResponse:
    horizon_days = max(1, min(horizon_days, 365))
    data = await asyncio.to_thread(decision_calendar.get_calendar, horizon_days=horizon_days)
    return JSONResponse(data)


@app.get("/api/implied")
async def api_implied(force: bool = False) -> JSONResponse:
    data = await asyncio.to_thread(implied_path.get_cached, force=force)
    return JSONResponse(data)


@app.get("/api/ois")
async def api_ois(months_ahead: int = 18, force: bool = False) -> JSONResponse:
    months_ahead = max(3, min(months_ahead, 36))
    data = await asyncio.to_thread(ois_curve.get_cached, months_ahead=months_ahead, force=force)
    return JSONResponse(data)


@app.get("/api/econ")
async def api_econ(force: bool = False) -> JSONResponse:
    data = await asyncio.to_thread(econ_releases.get_cached, force=force)
    return JSONResponse(data)


@app.get("/api/edge")
async def api_edge() -> JSONResponse:
    data = await asyncio.to_thread(edge_analysis.compute)
    return JSONResponse(data)


@app.get("/api/kalshi")
async def api_kalshi(force: bool = False) -> JSONResponse:
    """Raw Kalshi FOMC markets — useful for debugging the cross-venue join."""
    from datetime import date as _date, datetime as _dt, timezone as _tz

    today = _dt.now(_tz.utc).date()
    cal = await asyncio.to_thread(decision_calendar.upcoming, today, horizon_days=120)
    fomc = next((m for m in cal if m["cb"] == "US"), None)
    if not fomc:
        return JSONResponse({"meeting": None, "markets": []})
    md = _date.fromisoformat(fomc["decision_date"])
    rates = await asyncio.to_thread(fred_client.get_cached_rates)
    dff = next((s for s in rates["series"] if s["series_id"] == "DFF"), None)
    rate = dff["latest"][1] if dff and dff["latest"] else None
    markets = await asyncio.to_thread(kalshi_client.get_cached_for_meeting, md, rate, force=force)
    return JSONResponse(
        {
            "meeting": fomc,
            "current_rate": rate,
            "markets": markets,
        }
    )


@app.get("/api/stance")
async def api_stance() -> JSONResponse:
    data = await asyncio.to_thread(stance_analysis.compute)
    return JSONResponse(data)


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
        # C1: only honor the shared "dev-user" fallback when bound to
        # loopback. Otherwise multiple browsers would share one key.
        if _DEV_MODE and _DEV_MODE_LOOPBACK:
            return "dev-user"
        if _DEV_MODE:
            return JSONResponse(
                {"error": "DEV_MODE shared-user fallback disabled on non-loopback bind; either run with BIND_HOST=127.0.0.1 or supply X-Gateway-User-Id"},
                status_code=401,
            )
        return JSONResponse(
            {"error": "trading endpoints require gateway user identity"},
            status_code=401,
        )
    return uid


# M33: tight client_order_id charset — Kalshi rejects emoji/unicode anyway.
_CLIENT_OID_PATTERN = r"^[A-Za-z0-9_-]{1,80}$"
# M34: order id from the URL path (Kalshi UUIDs).
_ORDER_ID_PATTERN = r"^[A-Za-z0-9_-]{1,80}$"


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
    client_order_id: str | None = Field(default=None, max_length=80, pattern=_CLIENT_OID_PATTERN)


@app.get("/api/keys/kalshi/status")
async def api_key_status(request: Request) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    status = await asyncio.to_thread(key_store.status, uid)
    return JSONResponse(status)


@app.post("/api/keys/kalshi")
async def api_keys_upsert(request: Request, body: KalshiKeyPayload) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    try:
        await asyncio.to_thread(
            key_store.upsert_key,
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
    removed = await asyncio.to_thread(key_store.delete_key, uid)
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
    # H13: verify the stored credentials actually work against the new
    # host before flipping the mode. Otherwise users get cryptic 401s
    # later when they try to place an order.
    verified = await asyncio.to_thread(order_manager.verify_credentials_for_mode, uid, body.mode)
    if not verified.get("ok", False):
        return JSONResponse(
            {"ok": False, "error": f"credentials don't match {body.mode} host: {verified.get('error', 'unknown')}"},
            status_code=400,
        )
    ok = await asyncio.to_thread(key_store.set_mode, uid, body.mode)
    trade_audit.write_event(uid, "mode.set", ok=ok, mode=body.mode)
    return JSONResponse({"ok": ok, "mode": body.mode})


@app.get("/api/portfolio/balance")
async def api_balance(request: Request) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    data = await asyncio.to_thread(order_manager.get_balance, uid)
    return JSONResponse(data)


@app.get("/api/portfolio/positions")
async def api_positions(request: Request) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    data = await asyncio.to_thread(order_manager.get_positions, uid)
    return JSONResponse(data)


@app.get("/api/orders")
async def api_orders(request: Request) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    data = await asyncio.to_thread(order_manager.list_orders, uid)
    return JSONResponse(data)


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
    result = await order_manager.place_order_async(
        uid,
        ticker=body.ticker,
        side=body.side,
        action=body.action,
        count=body.count,
        price_cents=body.price_cents,
        client_order_id=body.client_order_id,
    )
    if result.get("duplicate"):
        return JSONResponse(result, status_code=409)
    if result.get("rate_limited"):
        return JSONResponse(result, status_code=429)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.delete("/api/order/kalshi/{order_id}")
async def api_order_cancel(
    request: Request,
    order_id: str = FPath(..., pattern=_ORDER_ID_PATTERN),
) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    result = await order_manager.cancel_order_async(uid, order_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/api/audit")
async def api_audit(request: Request, limit: int = 100) -> JSONResponse:
    uid = _require_user_id(request)
    if isinstance(uid, JSONResponse):
        return uid
    limit = max(1, min(limit, 500))
    events = await asyncio.to_thread(trade_audit.tail_for_user, uid, limit=limit)
    return JSONResponse({"events": events})


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("BIND_HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "7061")))
