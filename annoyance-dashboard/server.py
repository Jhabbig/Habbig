"""
Narve.ai Annoyance Dashboard — standalone FastAPI app.

Lives on port 8053, mirrors the world-state-dashboard pattern: single entry
point, lifespan-managed background loops, sqlite-backed, Chart.js frontend.

Background loops (all asyncio.create_task'd in lifespan):
  * reddit_loop       — every 600s, fetch r/new across config.REDDIT_SUBS
  * bluesky_loop      — every 600s, fetch AT Protocol searchPosts across
                        config.BLUESKY_SEARCH_TERMS (DECISIONS.md #13: 2nd source)
  * classifier_loop   — every 300s, drain unclassified posts via Claude
  * aggregator_loop   — every 900s, rebuild current+prev hour aggregates
  * spike_detector    — every 900s (offset 30s), detect + record spikes

All loops are try/except wrapped — transient errors log and continue, never
die. The dashboard boots even without ANTHROPIC_API_KEY (classifier no-ops).
"""

from __future__ import annotations

# ── Observability — init FIRST, before FastAPI touches anything ──────────────
from pathlib import Path as _Path

import observability
observability.init_sentry(platform="annoyance")
observability.configure_logging(base_dir=_Path(__file__).parent, service="annoyance")

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
import config
import db
import aggregator
import rate_limiter
import spike_detector
from classifier import classify_pending_posts
from sources.reddit import RedditSource
from sources.bluesky import BlueskySource


# Retention interval: raw content TTL loop runs every 6 hours.
# Matches DECISIONS.md #3 — classifications forever, raw content dropped at 30d.
RETENTION_LOOP_SECONDS = 6 * 60 * 60  # 6h
RETENTION_TTL_DAYS = 30

log = logging.getLogger("annoyance")


STATIC_DIR = Path(__file__).parent / "static"
reddit_source = RedditSource()
bluesky_source = BlueskySource()


# ── Background loops ─────────────────────────────────────────────────────────

async def reddit_loop() -> None:
    log.info("reddit_loop: starting, interval=%ds", config.REDDIT_LOOP_SECONDS)
    while True:
        try:
            posts = await reddit_source.fetch()
            new_count = 0
            for p in posts:
                if db.insert_post(
                    id=p["id"],
                    source=p["source"],
                    source_channel=p.get("source_channel"),
                    author=p.get("author"),
                    content=p["content"],
                    posted_at=p["posted_at"],
                    url=p.get("url"),
                    engagement=p.get("engagement", 0),
                    keyword=p.get("keyword"),
                ):
                    new_count += 1
            db.upsert_source_status(
                "reddit", ok=True, posts_today=new_count,
            )
            log.info("reddit_loop: fetched=%d new=%d", len(posts), new_count)
        except Exception as e:
            log.exception("reddit_loop: unhandled error")
            try:
                db.upsert_source_status("reddit", ok=False, error=str(e)[:500])
            except Exception:
                pass
        await asyncio.sleep(config.REDDIT_LOOP_SECONDS)


async def bluesky_loop() -> None:
    """Poll Bluesky's public search endpoint on the same cadence as Reddit.

    Independent of reddit_loop — one failing doesn't affect the other.
    Required by DECISIONS.md #13 for multi-source corroboration; the spike
    detector refuses to fire until it sees posts from >=2 sources (Reddit
    + Bluesky) about the same entity.
    """
    log.info("bluesky_loop: starting, interval=%ds", config.BLUESKY_LOOP_SECONDS)
    while True:
        try:
            posts = await bluesky_source.fetch()
            new_count = 0
            for p in posts:
                if db.insert_post(
                    id=p["id"],
                    source=p["source"],
                    source_channel=p.get("source_channel"),
                    author=p.get("author"),
                    content=p["content"],
                    posted_at=p["posted_at"],
                    url=p.get("url"),
                    engagement=p.get("engagement", 0),
                    keyword=p.get("keyword"),
                ):
                    new_count += 1
            db.upsert_source_status(
                "bluesky", ok=True, posts_today=new_count,
            )
            log.info("bluesky_loop: fetched=%d new=%d", len(posts), new_count)
        except Exception as e:
            log.exception("bluesky_loop: unhandled error")
            try:
                db.upsert_source_status("bluesky", ok=False, error=str(e)[:500])
            except Exception:
                pass
        await asyncio.sleep(config.BLUESKY_LOOP_SECONDS)


async def classifier_loop() -> None:
    log.info("classifier_loop: starting, interval=%ds", config.CLASSIFIER_LOOP_SECONDS)
    while True:
        try:
            summary = await classify_pending_posts(limit=config.CLASSIFIER_BATCH_SIZE)
            if summary.get("error") == "cost_ceiling":
                log.warning("classifier_loop: cost ceiling hit, will retry next tick")
            elif summary["triaged"] or summary["classified"] or summary["skipped"]:
                log.info(
                    "classifier_loop: triaged=%d classified=%d skipped=%d",
                    summary["triaged"], summary["classified"], summary["skipped"],
                )
            else:
                log.debug("classifier_loop: nothing to classify")
        except Exception:
            log.exception("classifier_loop: unhandled error")
        await asyncio.sleep(config.CLASSIFIER_LOOP_SECONDS)


async def retention_loop() -> None:
    """Raw-content TTL enforcement. Per DECISIONS.md #3, after 30 days we
    zero out posts.content + posts.author but keep the row + its
    classification so aggregates stay intact. JOINs in aggregator and
    spike_detector read classifications.entities_json (untouched) — the
    empty content string still joins cleanly."""
    log.info("retention_loop: starting, interval=%ds, ttl_days=%d",
             RETENTION_LOOP_SECONDS, RETENTION_TTL_DAYS)
    while True:
        try:
            scrubbed = db.scrub_raw_content_older_than(days=RETENTION_TTL_DAYS)
            if scrubbed:
                log.info("retention_loop: scrubbed %d posts (>%dd)",
                         scrubbed, RETENTION_TTL_DAYS)
            else:
                log.debug("retention_loop: nothing to scrub")
        except Exception:
            log.exception("retention_loop: unhandled error")
        await asyncio.sleep(RETENTION_LOOP_SECONDS)


async def aggregator_loop() -> None:
    log.info("aggregator_loop: starting, interval=%ds", config.AGGREGATOR_LOOP_SECONDS)
    while True:
        try:
            results = aggregator.rebuild_recent()
            for r in results:
                log.info("aggregator: %s posts=%d entities=%d index=%.1f",
                         r["hour"], r["posts"], r["entities"], r["index"])
        except Exception:
            log.exception("aggregator_loop: unhandled error")
        await asyncio.sleep(config.AGGREGATOR_LOOP_SECONDS)


async def spike_detector_loop() -> None:
    log.info("spike_detector_loop: starting, interval=%ds (offset=%ds)",
             config.SPIKE_DETECTOR_LOOP_SECONDS, config.SPIKE_DETECTOR_OFFSET_SECONDS)
    await asyncio.sleep(config.SPIKE_DETECTOR_OFFSET_SECONDS)  # let aggregator go first
    while True:
        try:
            fired = await spike_detector.detect_and_record()
            if fired:
                log.info("spike_detector: fired %d spikes", len(fired))
        except Exception:
            log.exception("spike_detector_loop: unhandled error")
        await asyncio.sleep(config.SPIKE_DETECTOR_LOOP_SECONDS)


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.assert_bound_to_localhost(config.HOST)
    log.info("annoyance dashboard: starting on %s:%d", config.HOST, config.PORT)
    db.init_db()
    log.info("db initialized at %s", config.DB_PATH)

    # Kill-switches (config.{REDDIT,BLUESKY,CLASSIFIER}_LOOP_ENABLED) let
    # staging keep Claude spend at $0 until launch-day while still building
    # backtest corpus from the free source loops. DB-only loops always run.
    tasks = []
    if config.REDDIT_LOOP_ENABLED:
        tasks.append(asyncio.create_task(reddit_loop(), name="reddit_loop"))
    else:
        log.warning("reddit_loop disabled via REDDIT_LOOP_ENABLED=false")
    if config.BLUESKY_LOOP_ENABLED:
        tasks.append(asyncio.create_task(bluesky_loop(), name="bluesky_loop"))
    else:
        log.warning("bluesky_loop disabled via BLUESKY_LOOP_ENABLED=false")
    if config.CLASSIFIER_ENABLED:
        tasks.append(asyncio.create_task(classifier_loop(), name="classifier_loop"))
    else:
        log.warning("classifier_loop disabled via CLASSIFIER_ENABLED=false")
    tasks.append(asyncio.create_task(aggregator_loop(), name="aggregator_loop"))
    tasks.append(asyncio.create_task(spike_detector_loop(), name="spike_detector_loop"))
    tasks.append(asyncio.create_task(retention_loop(), name="retention_loop"))
    try:
        yield
    finally:
        log.info("annoyance dashboard: shutting down, cancelling %d tasks", len(tasks))
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        log.info("shutdown complete")


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Narve.ai Annoyance Dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "db": str(config.DB_PATH),
        "has_api_key": bool(config.ANTHROPIC_API_KEY),
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


# ── JSON API ─────────────────────────────────────────────────────────────────

def _guard_api(request: Request) -> dict:
    """Every /api/* route (except /healthz) goes through this.

    Enforces the paywall AND the per-user (or per-ip) rate limit in one
    shot so we never double-count a request by forgetting either half.
    """
    user = auth.require_paid_user(request)
    rate_limiter.enforce(
        request, user,
        limit=rate_limiter.DEFAULT_API_LIMIT,
        window_seconds=rate_limiter.DEFAULT_API_WINDOW,
        scope="api",
    )
    return user


@app.get("/api/index")
async def api_index(request: Request, hours: int = 24) -> JSONResponse:
    """Time series of hourly annoyance index. Never 500s on empty — returns []."""
    _guard_api(request)
    hours = max(1, min(hours, 168 * 2))
    try:
        data = db.get_annoyance_index(hours=hours)
    except Exception:
        log.exception("/api/index failed")
        data = []
    return JSONResponse({"hours": data})


@app.get("/api/spikes")
async def api_spikes(request: Request, limit: int = 20) -> JSONResponse:
    _guard_api(request)
    limit = max(1, min(limit, 200))
    try:
        spikes = db.get_recent_spikes(limit=limit)
        # Hydrate sample posts WITH sensitivity flags so the client can blur
        # sensitive excerpts by default (DECISIONS.md #14). Falls back to the
        # cached sample_excerpts_json if the underlying post rows have been
        # scrubbed by the 30d retention loop — spike cards stay readable
        # either way.
        for s in spikes:
            ids = s.get("sample_post_ids") or []
            s["sample_posts"] = db.get_posts_with_sensitivity(ids)[:3]
    except Exception:
        log.exception("/api/spikes failed")
        spikes = []
    return JSONResponse({"spikes": spikes})


@app.get("/api/entities/top")
async def api_top_entities(request: Request, limit: int = 20) -> JSONResponse:
    """Top entities for the most recent hour WITH data, ranked by composite signal.

    Uses the latest hour that has entity_counts rows (not strictly current_hour)
    so this endpoint doesn't empty out in the first minutes of every new hour
    before the aggregator catches up.
    """
    _guard_api(request)
    limit = max(1, min(limit, 100))
    try:
        hour = db.get_latest_hour_with_entity_data()
        entities = db.get_top_entities_for_hour(hour, limit=limit) if hour else []
    except Exception:
        log.exception("/api/entities/top failed")
        entities = []
    return JSONResponse({"entities": entities, "hour": hour})


@app.get("/api/entity/{name}")
async def api_entity_detail(request: Request, name: str) -> JSONResponse:
    _guard_api(request)
    try:
        history = db.get_entity_history(name, hours=168)
    except Exception:
        log.exception("/api/entity/%s failed", name)
        history = []
    return JSONResponse({"entity": name, "history": history})


@app.get("/api/sources")
async def api_sources(request: Request) -> JSONResponse:
    _guard_api(request)
    try:
        sources = db.get_all_sources()
    except Exception:
        log.exception("/api/sources failed")
        sources = []
    return JSONResponse({"sources": sources})


@app.post("/api/fp-flag")
async def api_fp_flag(request: Request) -> JSONResponse:
    """False-positive feedback: user clicked the flag button on a spike/post.

    Rate-limited tighter than read endpoints (10/min) because each flag
    writes to a review queue and we don't want spam. Per DECISIONS.md #11
    these feed a review queue with no auto-tune.
    """
    user = auth.require_paid_user(request)
    rate_limiter.enforce(
        request, user,
        limit=rate_limiter.FP_FLAG_LIMIT,
        window_seconds=rate_limiter.FP_FLAG_WINDOW,
        scope="fp_flag",
    )
    try:
        body = await request.json()
    except Exception:
        body = {}
    target_id = (body.get("target_id") or "").strip()
    target_type = (body.get("target_type") or "spike").strip()
    reason = (body.get("reason") or "")[:500]
    if not target_id:
        raise HTTPException(status_code=400, detail="target_id required")
    if target_type != "spike":
        raise HTTPException(status_code=400, detail="only spike targets are supported")
    try:
        spike_id = int(target_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="target_id must be an integer spike_id")
    try:
        db.insert_fp_flag(
            spike_id=spike_id,
            user_id=str(user["id"]),
            user_email=user.get("email"),
            reason=reason,
        )
    except Exception:
        log.exception("/api/fp-flag write failed")
    return JSONResponse({"ok": True})


# ── Admin (super_admin + localhost) ──────────────────────────────────────────

@app.post("/admin/trigger")
async def admin_trigger(request: Request, loop: str) -> JSONResponse:
    """
    Manually run one of the background tasks once.

    loop ∈ {reddit, classifier, aggregator, spike_detector, retention}
    """
    auth.require_admin(request)

    if loop == "reddit":
        posts = await reddit_source.fetch()
        new_count = 0
        for p in posts:
            if db.insert_post(
                id=p["id"], source=p["source"],
                source_channel=p.get("source_channel"),
                author=p.get("author"), content=p["content"],
                posted_at=p["posted_at"], url=p.get("url"),
                engagement=p.get("engagement", 0), keyword=p.get("keyword"),
            ):
                new_count += 1
        db.upsert_source_status("reddit", ok=True, posts_today=new_count)
        return JSONResponse({"loop": "reddit", "fetched": len(posts), "new": new_count})

    if loop == "classifier":
        summary = await classify_pending_posts(limit=config.CLASSIFIER_BATCH_SIZE)
        return JSONResponse({"loop": "classifier", **summary})

    if loop == "retention":
        scrubbed = db.scrub_raw_content_older_than(days=RETENTION_TTL_DAYS)
        return JSONResponse({"loop": "retention", "scrubbed": scrubbed, "ttl_days": RETENTION_TTL_DAYS})

    if loop == "aggregator":
        results = aggregator.rebuild_recent()
        return JSONResponse({"loop": "aggregator", "results": results})

    if loop == "spike_detector":
        fired = await spike_detector.detect_and_record()
        return JSONResponse({"loop": "spike_detector", "fired": fired})

    raise HTTPException(status_code=400, detail="unknown loop")


@app.get("/admin/cost-summary")
async def admin_cost_summary(request: Request, days: int = 7) -> JSONResponse:
    """Per-day, per-model, per-operation Claude cost breakdown.

    Plus: today-so-far against DAILY_COST_CEILING_CENTS. Gated on localhost —
    the gateway exposes this to the P4 admin panel via the normal SSO header
    wrap. Never 500s on an empty table.
    """
    auth.require_admin(request)
    days = max(1, min(days, 90))
    try:
        rows = db.cost_summary(days=days)
    except Exception:
        log.exception("/admin/cost-summary failed")
        rows = []
    try:
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        today_cents = db.cost_cents_since(today_start)
    except Exception:
        today_cents = 0.0
    return JSONResponse({
        "ceiling_cents": config.DAILY_COST_CEILING_CENTS,
        "today_cents": round(today_cents, 3),
        "ceiling_exceeded": today_cents >= config.DAILY_COST_CEILING_CENTS,
        "by_day_model_op": rows,
    })


@app.post("/admin/reclassify")
async def admin_reclassify(request: Request, limit: int = 100) -> JSONResponse:
    """Reset classified=0 on the N most recent classified posts so they get reprocessed."""
    auth.require_admin(request)
    with db.cursor() as cur:
        cur.execute(
            """UPDATE posts SET classified = 0
               WHERE id IN (
                 SELECT id FROM posts
                 WHERE classified IN (1, 2)
                 ORDER BY posted_at DESC
                 LIMIT ?
               )""",
            (limit,),
        )
        affected = cur.rowcount
    return JSONResponse({"reset": affected})


# ── FP review queue (super_admin + localhost) ────────────────────────────────

@app.get("/admin/fp-queue")
async def admin_fp_queue(request: Request, resolved: bool = False, limit: int = 50) -> JSONResponse:
    """List false-positive flags. Unresolved by default.

    Joined with the spike that was flagged so the reviewer sees entity /
    summary / z_score inline, without a second query per flag.
    """
    auth.require_admin(request)
    limit = max(1, min(limit, 500))
    try:
        flags = db.list_fp_queue(resolved=resolved, limit=limit)
    except Exception:
        log.exception("/admin/fp-queue failed")
        flags = []
    return JSONResponse({"flags": flags})


@app.post("/admin/fp-resolve")
async def admin_fp_resolve(request: Request) -> JSONResponse:
    """Mark a FP flag resolved. Body: ``{"flag_id": int, "note": str?}``."""
    auth.require_admin(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        flag_id = int(body.get("flag_id") or 0)
    except (TypeError, ValueError):
        flag_id = 0
    if flag_id <= 0:
        raise HTTPException(status_code=400, detail="flag_id required")
    note = (body.get("note") or "")[:500] or None
    try:
        ok = db.resolve_fp_flag(flag_id, note=note)
    except Exception:
        log.exception("/admin/fp-resolve failed")
        ok = False
    if not ok:
        raise HTTPException(status_code=404, detail="flag not found or already resolved")
    return JSONResponse({"ok": True})


@app.get("/admin")
async def admin_page(request: Request) -> FileResponse:
    """Admin panel — minimal FP-review queue. Gated on localhost +
    super_admin per the auth module (assert_bound_to_localhost means the
    gateway never proxies this path)."""
    auth.require_admin(request)
    return FileResponse(str(STATIC_DIR / "admin.html"))


# ── Entity drill-in ──────────────────────────────────────────────────────────

@app.get("/entity/{name}")
async def entity_page(request: Request, name: str) -> FileResponse:
    """HTML drill-in page. The page's JS loads /api/entity/{name}* for its
    four panels (history, spikes, recent posts, markets). Gated on paid
    tier — the drill-in detail is a Pro feature."""
    auth.require_paid_user(request)
    return FileResponse(str(STATIC_DIR / "entity.html"))


@app.get("/api/entity/{name}/spikes")
async def api_entity_spikes(request: Request, name: str, limit: int = 20) -> JSONResponse:
    _guard_api(request)
    limit = max(1, min(limit, 100))
    try:
        spikes = db.get_entity_spikes(name, limit=limit)
    except Exception:
        log.exception("/api/entity/%s/spikes failed", name)
        spikes = []
    return JSONResponse({"entity": name, "spikes": spikes})


@app.get("/api/entity/{name}/recent-posts")
async def api_entity_recent_posts(request: Request, name: str, limit: int = 30) -> JSONResponse:
    _guard_api(request)
    limit = max(1, min(limit, 200))
    try:
        posts = db.get_entity_recent_classified_posts(name, limit=limit)
    except Exception:
        log.exception("/api/entity/%s/recent-posts failed", name)
        posts = []
    return JSONResponse({"entity": name, "posts": posts})


_ENTITY_MARKETS_PATH = Path(__file__).parent / "entity_markets.json"
_ENTITY_MARKETS_CACHE: Optional[dict] = None


def _load_entity_markets() -> dict:
    """Read entity_markets.json once per process. The file is scaffolded by
    ``scripts/build_entity_markets.py``; curators overwrite individual
    entries post-merge. Restart the server to pick up a new edit."""
    global _ENTITY_MARKETS_CACHE
    if _ENTITY_MARKETS_CACHE is not None:
        return _ENTITY_MARKETS_CACHE
    try:
        import json as _json
        if _ENTITY_MARKETS_PATH.exists():
            _ENTITY_MARKETS_CACHE = _json.loads(_ENTITY_MARKETS_PATH.read_text())
        else:
            _ENTITY_MARKETS_CACHE = {}
    except Exception:
        log.exception("entity_markets.json parse failed")
        _ENTITY_MARKETS_CACHE = {}
    return _ENTITY_MARKETS_CACHE


@app.get("/api/entity/{name}/markets")
async def api_entity_markets(request: Request, name: str) -> JSONResponse:
    _guard_api(request)
    markets = _load_entity_markets().get(name, [])
    return JSONResponse({"entity": name, "markets": markets})


_MARKET_SUGGESTIONS_LOG = Path(__file__).parent / "market_suggestions.log"


@app.post("/api/market-suggestions")
async def api_market_suggestions(request: Request) -> JSONResponse:
    """User-submitted market URL suggestions for entities we don't have
    curated entries for yet. Logs to ``market_suggestions.log`` for v1 —
    community moderation workflow is out of scope for polish.

    Body: ``{"entity": str, "url": str?, "note": str?}``
    """
    user = _guard_api(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    entity = (body.get("entity") or "").strip()[:200]
    url = (body.get("url") or "").strip()[:500]
    note = (body.get("note") or "").strip()[:500]
    if not entity:
        raise HTTPException(status_code=400, detail="entity required")
    line = (
        f"{datetime.now(timezone.utc).isoformat()}\t"
        f"user={user.get('id')}\t"
        f"email={user.get('email')}\t"
        f"entity={entity}\t"
        f"url={url}\t"
        f"note={note}\n"
    )
    try:
        with _MARKET_SUGGESTIONS_LOG.open("a") as f:
            f.write(line)
    except Exception:
        log.exception("/api/market-suggestions log write failed")
    return JSONResponse({"ok": True})


# ── Auth status (public — no paywall) ────────────────────────────────────────

@app.get("/api/me")
async def api_me(request: Request) -> JSONResponse:
    """Current auth state. Used by the client to toggle the paywall banner
    and enable authenticated-only buttons (like the ⚑ flag). Never 402s —
    anonymous users get ``{"authenticated": false}`` so the UI can render
    a graceful upgrade CTA instead of an error.
    """
    user = auth.get_session_user(request)
    if not user:
        return JSONResponse({"authenticated": False})
    return JSONResponse({
        "authenticated": True,
        "user_id": user.get("id"),
        "email": user.get("email"),
        "tier": user.get("tier"),
    })


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    auth.assert_bound_to_localhost(config.HOST)
    uvicorn.run(
        "server:app",
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        reload=False,
    )
