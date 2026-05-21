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
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402

import backtest                                                # noqa: E402
import cache                                                   # noqa: E402
import dedup                                                   # noqa: E402
import digest as digest_mod                                    # noqa: E402
import edge                                                    # noqa: E402
import export as export_mod                                    # noqa: E402
import index_calc                                              # noqa: E402
import source_quality                                          # noqa: E402
import surge_calc                                              # noqa: E402
from models import SECTIONS                                    # noqa: E402
from scheduler import (                                        # noqa: E402
    Scheduler, digest_worker, index_history_worker, phash_worker, surge_worker,
)
from scrapers import registry                                  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Culture Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — all requests will 503")

# Scheduler singleton + auxiliary workers — populated on startup.
_scheduler: Scheduler | None = None
_workers_stop = asyncio.Event()
_worker_tasks: list[asyncio.Task] = []


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
    _worker_tasks.append(asyncio.create_task(
        phash_worker(_workers_stop), name="culture-phash"))
    _worker_tasks.append(asyncio.create_task(
        index_history_worker(_workers_stop), name="culture-history"))
    _worker_tasks.append(asyncio.create_task(
        surge_worker(_workers_stop), name="culture-surges"))
    _worker_tasks.append(asyncio.create_task(
        digest_worker(_workers_stop), name="culture-digest"))


@app.on_event("shutdown")
async def on_shutdown() -> None:
    _workers_stop.set()
    if _scheduler:
        await _scheduler.stop()
    for t in _worker_tasks:
        try:
            await asyncio.wait_for(t, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/topic/{slug}", response_class=HTMLResponse)
async def topic_page(slug: str) -> HTMLResponse:
    # Same SPA shell — the front-end router reads location.pathname and
    # renders the topic-detail view instead of the main grid.
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/api/topic/{slug}")
async def api_topic(slug: str, days: int = 30) -> JSONResponse:
    days = max(1, min(days, 90))
    snapshots = cache.topic_snapshots_by_label(slug, days=days)
    if not snapshots:
        return JSONResponse({
            "slug": slug, "label": slug, "snapshots": [],
            "stats": None, "current": None,
        })
    # Stats across the window.
    surges = [s["surge_signal"] for s in snapshots if s.get("surge_signal") is not None]
    stats = {
        "first_seen": snapshots[0]["ts"],
        "last_seen": snapshots[-1]["ts"],
        "total_snapshots": len(snapshots),
        "peak_spread": max(s["spread"] for s in snapshots),
        "peak_surge": max(surges) if surges else None,
    }
    # Try to surface the live cluster that matches this label right now.
    current = None
    for t in edge.compute_topics_with_markets(limit=50):
        if t["label"] == slug:
            current = {
                "spread": t["spread"],
                "sources": t["sources"],
                "sections": t["sections"],
                "surge_signal": t.get("surge_signal"),
                "items": [{"title": i["title"], "url": i.get("url"),
                           "source": i["source"]} for i in t["items"][:10]],
                "markets": t.get("markets", []),
            }
            break
    return JSONResponse({
        "slug": slug, "label": snapshots[-1]["label"],
        "snapshots": snapshots, "stats": stats, "current": current,
    })


@app.get("/api/index")
async def api_index() -> JSONResponse:
    return JSONResponse(index_calc.compute())


@app.get("/api/index/history")
async def api_index_history(hours: int = 72) -> JSONResponse:
    hours = max(1, min(hours, 24 * 30))
    return JSONResponse({"hours": hours, "points": cache.index_history(hours)})


@app.get("/api/surges")
async def api_surges(limit: int = 20) -> JSONResponse:
    limit = max(1, min(limit, 100))
    return JSONResponse({
        "items": surge_calc.compute(limit=limit),
        "threshold": surge_calc.webhook_threshold(),
    })


@app.get("/api/topics")
async def api_topics(limit: int = 20) -> JSONResponse:
    limit = max(1, min(limit, 100))
    return JSONResponse({"topics": edge.compute_topics_with_markets(limit=limit)})


@app.get("/api/edges")
async def api_edges(limit: int = 20) -> JSONResponse:
    limit = max(1, min(limit, 100))
    return JSONResponse({"edges": edge.compute_edges(limit=limit)})


@app.get("/api/digest")
async def api_digest() -> JSONResponse:
    return JSONResponse({
        "digest": cache.latest_digest(),
        "configured": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "default_model": digest_mod.DEFAULT_MODEL,
    })


@app.get("/api/backtest")
async def api_backtest(
    days: int = 30,
    limit: int = 50,
    threshold_pct: float | None = None,
    window_hours: int | None = None,
) -> JSONResponse:
    days = max(1, min(days, 365))
    limit = max(1, min(limit, 500))
    if threshold_pct is not None:
        threshold_pct = max(0.001, min(threshold_pct, 0.5))
    if window_hours is not None:
        window_hours = max(1, min(window_hours, 24 * 14))
    return JSONResponse(backtest.validate(
        days=days, limit=limit,
        threshold_pct=threshold_pct, window_hours=window_hours,
    ))


@app.get("/compare", response_class=HTMLResponse)
async def compare_page() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/export", response_class=HTMLResponse)
async def export_page() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/api/headlines")
async def api_headlines(days: int = 30) -> JSONResponse:
    days = max(1, min(days, 365))
    return JSONResponse({"days": days, "headlines": cache.daily_headlines(days)})


@app.get("/api/source_quality")
async def api_source_quality(days: int = 30) -> JSONResponse:
    days = max(1, min(days, 365))
    return JSONResponse(source_quality.compute(days=days))


@app.get("/api/export")
async def api_export(
    type: str | None = None,
    days: int = 30,
    format: str = "csv",
):
    days = max(1, min(days, 365))
    if type is None:
        return JSONResponse({"types": export_mod.available_types()})
    if type not in export_mod.available_types():
        raise HTTPException(404, f"unknown export type: {type}")
    if format == "json":
        return JSONResponse(export_mod.as_json(type, days))
    if format != "csv":
        raise HTTPException(400, "format must be csv or json")
    filename = f"culture_{type}_{days}d.csv"
    return StreamingResponse(
        export_mod.stream_csv(type, days),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/digest/refresh")
async def api_digest_refresh() -> JSONResponse:
    async def _run() -> None:
        d = await asyncio.to_thread(digest_mod.generate)
        if d:
            cache.record_digest(d)
    asyncio.create_task(_run())
    return JSONResponse({"queued": True})


@app.get("/api/section/{section}")
async def api_section(section: str, limit: int = 50, dedup_results: bool = True) -> JSONResponse:
    if section not in SECTIONS:
        raise HTTPException(404, f"unknown section: {section}")
    limit = max(1, min(limit, 200))
    # Pull more than `limit` so dedup doesn't shrink the list below the cap.
    raw = cache.get_section(section, min(limit * 2, 200))
    items = dedup.cluster_items(raw) if dedup_results else raw
    return JSONResponse({"section": section, "items": items[:limit]})


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
