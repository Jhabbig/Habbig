#!/usr/bin/env python3
"""Culture Dashboard — FastAPI backend.

v0 surface:
  - GET /                 → index.html
  - GET /api/index        → composite culture index (overall + per-section)
  - GET /api/section/{s}  → top items in a section
  - GET /api/source/{s}   → top items from one source (debug / drilldown)
  - POST /api/refresh     → kick all scrapers (or one via ?source=)
  - GET /api/health       → status + per-source freshness
  - GET /healthz          → liveness (unauthenticated)

Auth: same gateway-SSO pattern as world-state / centralbank.
Set DEV_MODE=1 to bypass when running locally.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
from pathlib import Path

# Make `from models import Item` and `from scrapers import ...` work whether
# we're run via `python server.py` or `uvicorn server:app`.
sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, HTTPException, Request           # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse      # noqa: E402

import cache                                                   # noqa: E402
import index_calc                                              # noqa: E402
from models import SECTIONS                                    # noqa: E402
from scheduler import Scheduler                                # noqa: E402
from scrapers import registry                                  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Culture Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — all requests will 503")

# Scheduler singleton — populated on startup.
_scheduler: Scheduler | None = None


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
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.on_event("startup")
async def on_startup() -> None:
    db_path = os.environ.get("CULTURE_DB_PATH")
    if db_path:
        cache.set_db_path(db_path)
    cache.init_db()
    global _scheduler
    _scheduler = Scheduler(registry())
    await _scheduler.start()
    # Kick a first refresh in the background so the dashboard isn't empty
    # for new deploys. Don't block startup on it.
    asyncio.create_task(_scheduler.run_once())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if _scheduler:
        await _scheduler.stop()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/api/index")
async def api_index() -> JSONResponse:
    return JSONResponse(index_calc.compute())


@app.get("/api/section/{section}")
async def api_section(section: str, limit: int = 50) -> JSONResponse:
    if section not in SECTIONS:
        raise HTTPException(404, f"unknown section: {section}")
    limit = max(1, min(limit, 200))
    return JSONResponse({"section": section, "items": cache.get_section(section, limit)})


@app.get("/api/source/{source}")
async def api_source(source: str, limit: int = 50) -> JSONResponse:
    limit = max(1, min(limit, 200))
    return JSONResponse({"source": source, "items": cache.get_source(source, limit)})


@app.post("/api/refresh")
async def api_refresh(source: str | None = None) -> JSONResponse:
    if not _scheduler:
        raise HTTPException(503, "scheduler not ready")
    asyncio.create_task(_scheduler.run_once(only=source))
    return JSONResponse({"queued": True, "source": source})


@app.get("/api/health")
async def api_health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "sources": cache.list_runs(),
        "sections": list(SECTIONS),
    })


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "7070")),
    )
