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

from analysis import edge as edge_analysis
from analysis import stance as stance_analysis
from ingestion import decision_calendar, econ_releases, fred_client, implied_path, kalshi_client, ois_curve

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


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "7060")))
