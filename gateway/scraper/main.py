"""
Scraper service — FastAPI application.

Exposes endpoints for:
  1. Main server to pull data on demand
  2. Admin panel to manage the scraper remotely

All endpoints require Authorization: Bearer {SCRAPER_API_KEY}

LEGAL NOTE: This scraper service is intended for personal/research use only.
Users should ensure compliance with each platform's Terms of Service.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import logging.handlers  # kept for legacy import compatibility
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

from scraper.config import SCRAPER_API_KEY, LOG_LEVEL, LOG_FILE
from scraper.observability import init_sentry
from scraper.storage import db as store

# Initialize Sentry BEFORE FastAPI. Uses the same scrubber as the gateway.
_SENTRY_ACTIVE = init_sentry(platform="scraper")
from scraper.scheduler import (
    start_scheduler, get_scheduler_status,
    pause_job, resume_job, trigger_job, update_job_interval,
    twitter_scraper, truthsocial_scraper,
    run_twitter_scrape, run_truthsocial_scrape,
)
from scraper.transmission.pusher import push_untransmitted
from scraper.transmission.receiver import pull_jobs

# ── Logging ──────────────────────────────────────────────────────────────────
# Centralised structured-JSON logging. SERVICE_NAME=scraper routes to
# LOGTAIL_TOKEN_SCRAPER when BetterStack is configured.

os.environ.setdefault("SERVICE_NAME", "scraper")

# Ensure the legacy scraper log directory still exists — the old rotating
# file handler wrote there and some operators may tail it. configure_logging()
# also creates a logs/ dir next to logging_config.py, but that's a different
# path, so we keep this for backwards compatibility.
log_dir = Path(LOG_FILE).parent
log_dir.mkdir(parents=True, exist_ok=True)

# Resolve the gateway repo root so configure_logging() writes into the same
# logs/ directory as the main app — one place to tail everything.
_GATEWAY_ROOT = Path(__file__).resolve().parent.parent

from logging_config import configure_logging, get_logger  # noqa: E402
configure_logging(base_dir=_GATEWAY_ROOT)
log = get_logger("scraper")

# ── App startup ──────────────────────────────────────────────────────────────

START_TIME = time.monotonic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    log.info("Database initialised at %s", store.SCRAPER_DB_PATH)
    start_scheduler()
    yield
    log.info("Scraper service shutting down")


app = FastAPI(title="Narve.ai Scraper Service", version="1.0.0", lifespan=lifespan)


# ── Auth dependency ──────────────────────────────────────────────────────────

def require_api_key(request: Request) -> None:
    """Validate SCRAPER_API_KEY using constant-time comparison."""
    if not SCRAPER_API_KEY:
        raise HTTPException(status_code=500, detail="SCRAPER_API_KEY not configured")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth[7:]
    if not hmac.compare_digest(token, SCRAPER_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Health & status ──────────────────────────────────────────────────────────

@app.get("/health", dependencies=[Depends(require_api_key)])
async def health():
    twitter_health = await twitter_scraper.health_check()
    truthsocial_health = await truthsocial_scraper.health_check()

    twitter_ok = twitter_health["available"]
    truthsocial_ok = truthsocial_health["available"]

    if twitter_ok and truthsocial_ok:
        status = "ok"
    elif twitter_ok or truthsocial_ok:
        status = "degraded"
    else:
        status = "error"

    sched = get_scheduler_status()

    return {
        "status": status,
        "uptime_seconds": round(time.monotonic() - START_TIME, 1),
        "twitter": twitter_health,
        "truthsocial": truthsocial_health,
        "untransmitted_count": store.get_untransmitted_count(),
        "scheduler_running": sched["running"],
    }


# ── On-demand pull ───────────────────────────────────────────────────────────

@app.post("/pull", dependencies=[Depends(require_api_key)])
async def pull_start(request: Request):
    body = await request.json()
    platform = body.get("platform", "all")
    keywords = body.get("keywords")

    job = pull_jobs.create(platform, keywords)

    async def _run_pull():
        try:
            total = 0
            if platform in ("twitter", "all"):
                await run_twitter_scrape()
                total += store.get_posts_today_count("twitter")
            if platform in ("truthsocial", "all"):
                await run_truthsocial_scrape()
                total += store.get_posts_today_count("truthsocial")
            pull_jobs.complete(job.job_id, total)
        except Exception as e:
            pull_jobs.fail(job.job_id, str(e))

    asyncio.ensure_future(_run_pull())

    return {
        "job_id": job.job_id,
        "estimated_completion_seconds": 120 if platform == "all" else 60,
    }


@app.get("/pull/{job_id}", dependencies=[Depends(require_api_key)])
async def pull_status(job_id: str):
    job = pull_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


# ── Data access ──────────────────────────────────────────────────────────────

@app.get("/posts/untransmitted", dependencies=[Depends(require_api_key)])
async def posts_untransmitted(platform: Optional[str] = None, limit: int = 100):
    posts = store.get_untransmitted(platform=platform, limit=limit)
    return [p.to_dict() for p in posts]


@app.post("/posts/acknowledge", dependencies=[Depends(require_api_key)])
async def posts_acknowledge(request: Request):
    body = await request.json()
    post_ids = body.get("post_ids", [])
    updated = store.mark_transmitted(post_ids)
    return {"acknowledged": updated}


# ── Scheduler management ────────────────────────────────────────────────────

@app.get("/scheduler/status", dependencies=[Depends(require_api_key)])
async def scheduler_status():
    return get_scheduler_status()


@app.post("/scheduler/pause/{job_id}", dependencies=[Depends(require_api_key)])
async def scheduler_pause(job_id: str):
    ok = pause_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"paused": True}


@app.post("/scheduler/resume/{job_id}", dependencies=[Depends(require_api_key)])
async def scheduler_resume(job_id: str):
    ok = resume_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"resumed": True}


@app.post("/scheduler/trigger/{job_id}", dependencies=[Depends(require_api_key)])
async def scheduler_trigger(job_id: str):
    ok = trigger_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"triggered": True, "job_id": job_id}


@app.patch("/scheduler/interval/{job_id}", dependencies=[Depends(require_api_key)])
async def scheduler_interval(job_id: str, request: Request):
    body = await request.json()
    minutes = body.get("interval_minutes")
    if not minutes or minutes < 1:
        raise HTTPException(status_code=400, detail="interval_minutes must be >= 1")
    ok = update_job_interval(job_id, minutes)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"updated": True, "new_interval_minutes": minutes}


# ── Keyword management ──────────────────────────────────────────────────────

@app.get("/keywords", dependencies=[Depends(require_api_key)])
async def keywords_list():
    return store.get_keywords()


@app.post("/keywords", dependencies=[Depends(require_api_key)])
async def keywords_add(request: Request):
    body = await request.json()
    platform = body.get("platform", "")
    keyword = body.get("keyword", "").strip()
    if not platform or not keyword:
        raise HTTPException(status_code=400, detail="platform and keyword required")
    if platform not in ("twitter", "truthsocial"):
        raise HTTPException(status_code=400, detail="platform must be 'twitter' or 'truthsocial'")
    added = store.add_keyword(platform, keyword)
    return {"added": added}


@app.delete("/keywords/{platform}/{keyword}", dependencies=[Depends(require_api_key)])
async def keywords_delete(platform: str, keyword: str):
    deleted = store.remove_keyword(platform, keyword)
    if not deleted:
        raise HTTPException(status_code=404, detail="Keyword not found")
    return {"deleted": True}


# ── Session management ──────────────────────────────────────────────────────

@app.get("/sessions", dependencies=[Depends(require_api_key)])
async def sessions_list():
    sessions = store.list_sessions()
    return [s.to_dict() for s in sessions]


@app.post("/sessions/validate/{platform}", dependencies=[Depends(require_api_key)])
async def sessions_validate(platform: str):
    if platform == "twitter":
        valid = twitter_scraper.is_available()
    elif platform == "truthsocial":
        valid = truthsocial_scraper.is_available()
    else:
        raise HTTPException(status_code=400, detail="Invalid platform")

    session = store.get_session(platform)
    return {
        "valid": valid,
        "session_exists": session is not None,
        "last_used_at": session.last_used_at.isoformat() if session and session.last_used_at else None,
    }


@app.post("/sessions/reset/{platform}", dependencies=[Depends(require_api_key)])
async def sessions_reset(platform: str):
    if platform not in ("twitter", "truthsocial"):
        raise HTTPException(status_code=400, detail="Invalid platform")
    store.delete_session(platform)
    # Also clear the profile directory
    from scraper.config import SESSION_PROFILE_PATH
    import shutil
    profile_dir = Path(SESSION_PROFILE_PATH) / platform
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
    return {"reset": True}


# ── Run history ──────────────────────────────────────────────────────────────

@app.get("/runs", dependencies=[Depends(require_api_key)])
async def runs_list(platform: Optional[str] = None, limit: int = 50):
    runs = store.list_runs(platform=platform, limit=limit)
    return [r.to_dict() for r in runs]


@app.get("/runs/{run_id}", dependencies=[Depends(require_api_key)])
async def runs_get(run_id: int):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run.to_dict()


# ── Config ───────────────────────────────────────────────────────────────────

@app.get("/config", dependencies=[Depends(require_api_key)])
async def config_get():
    from scraper import config as cfg
    return {
        "twitter_interval_minutes": cfg.TWITTER_INTERVAL_MINUTES,
        "truthsocial_interval_minutes": cfg.TRUTHSOCIAL_INTERVAL_MINUTES,
        "retry_transmission_interval_minutes": cfg.RETRY_TRANSMISSION_INTERVAL_MINUTES,
        "max_posts_per_keyword": cfg.MAX_POSTS_PER_KEYWORD,
        "max_transmission_attempts": cfg.MAX_TRANSMISSION_ATTEMPTS,
        "twitter_delay_between_keywords": cfg.TWITTER_DELAY_BETWEEN_KEYWORDS,
        "truthsocial_delay_between_keywords": cfg.TRUTHSOCIAL_DELAY_BETWEEN_KEYWORDS,
        "playwright_headless": cfg.PLAYWRIGHT_HEADLESS,
        "browser_type": cfg.BROWSER_TYPE,
        "truthsocial_prominent_accounts": cfg.TRUTHSOCIAL_PROMINENT_ACCOUNTS,
    }


@app.patch("/config", dependencies=[Depends(require_api_key)])
async def config_update(request: Request):
    body = await request.json()
    key = body.get("key", "")
    value = body.get("value")

    # Runtime-updatable config stored in state file
    from scraper.config import load_runtime_state, save_runtime_state
    state = load_runtime_state()
    state[key] = value
    save_runtime_state(state)

    return {"updated": True}
