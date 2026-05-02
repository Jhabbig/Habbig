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
from ingestion import decision_calendar, fred_client, implied_path

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


@app.get("/api/edge")
async def api_edge() -> JSONResponse:
    return JSONResponse(edge_analysis.compute())


@app.get("/api/stance")
async def api_stance() -> JSONResponse:
    return JSONResponse(stance_analysis.compute())


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7060")))
