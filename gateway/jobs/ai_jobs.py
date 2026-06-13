"""Cron + on-demand jobs that drive the Claude-backed intelligence features.

Four registered jobs, all chunked so one run can't blow past the daily
spend budget:

  run_extract_for_recent_posts  - post-scrape Claude extraction pass
  reextract_all_predictions     - one-shot backfill (admin-triggered)
  categorise_uncached_markets   - fills market_categorisations from cron
  regenerate_stale_source_summaries - monthly Sonnet pass

The fifth job (``check_daily_claude_spend``) lives in
``jobs/claude_cost_check.py`` - that module owns the kill-switch path
that wires into ``ai.client.set_kill_switch`` and the deduped
``claude_cost_alerts`` table, both absent from the previous in-line
copy. Moved to jobs/claude_cost_check.py as part of Fix C duplicate
removal.

Each job logs a compact result dict so the admin's "last run" link in the
AI-usage panel shows something useful.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.ai")


EXTRACTION_CHUNK_SIZE = 100
CATEGORISATION_CHUNK_SIZE = 50
SUMMARY_CHUNK_SIZE = 25
DEFAULT_DAILY_SPEND_THRESHOLD_USD = float(os.environ.get("CLAUDE_DAILY_SPEND_THRESHOLD_USD", "50"))


# ── 1. Extract recent posts into structured predictions ─────────────────────


@register_job("run_extract_for_recent_posts")
async def run_extract_for_recent_posts(
    posts: list[dict] | None = None,
    limit: int = EXTRACTION_CHUNK_SIZE,
) -> dict[str, Any]:
    """Drain freshly-scraped posts into the extraction pipeline.

    Reality of the ingest path (verified, not assumed): the standalone
    scraper service keeps its raw posts in its OWN local SQLite DB
    (``scraper/storage/db.py`` -> ``SCRAPER_DB_PATH``) and pushes batches
    over HTTP to ``POST /api/scraper/ingest`` (``scraper_routes.py``).
    That endpoint extracts **synchronously** — it awaits
    ``queries.scraper_ingest.ingest_and_extract`` -> per post
    ``pipeline.extract_step.process_post`` — and writes predictions
    inline before returning 200 to the scraper.

    Consequence: there is NO gateway-side store of "recently-ingested,
    not-yet-extracted" posts for this cron to query. The main DB has no
    ``raw_posts`` / ``social_posts`` table (the only ``raw_posts`` table
    lives in the scraper's local DB, on a different process/host). So
    when this job fires on its cron with no explicit batch, the honest
    answer is: there is nothing to do. We log that and return rather than
    pretending to have processed work.

    The job still accepts an explicit ``posts`` batch (a list of
    ``{content, author_handle, post_id?, source_url?}`` dicts) so a direct
    caller can drive extraction on demand. When given one, we route each
    post through the SAME entrypoint the live ingest uses —
    ``pipeline.extract_step.process_post`` — so the behaviour is identical
    to the synchronous ingest path (no divergent second extractor).

    Returns a counter dict for the admin AI-usage panel.
    """
    batch = list(posts or [])[: int(limit)]

    if not batch:
        # No gateway-side queue of unprocessed posts exists; ingest already
        # extracted everything inline. Nothing for the cron to do.
        result = {
            "considered": 0,
            "predictions_written": 0,
            "errors": 0,
            "noop_reason": "no_explicit_batch_and_ingest_extracts_inline",
        }
        log.info("extractor pass: nothing to do (%s)", result)
        return result

    # Explicit batch supplied — extract via the canonical ingest entrypoint.
    from pipeline.extract_step import process_post

    considered = 0
    predictions_written = 0
    errors = 0
    for post in batch:
        content = post.get("content") or ""
        handle = post.get("author_handle") or ""
        if not content.strip() or not handle:
            continue
        considered += 1
        try:
            r = await process_post({
                "post_id": post.get("post_id") or post.get("id"),
                "content": content,
                "author_handle": handle,
                "source_url": post.get("source_url"),
            })
            predictions_written += int(r.get("predictions_written", 0) or 0)
        except Exception:  # one bad post must not fail the batch
            log.exception(
                "extraction failed for post %s",
                post.get("post_id") or post.get("id"),
            )
            errors += 1

    result = {
        "considered": considered,
        "predictions_written": predictions_written,
        "errors": errors,
    }
    log.info("extractor pass: %s", result)
    return result


# ── 2. Backfill: re-extract every existing prediction ────────────────────────


@register_job("reextract_all_predictions")
async def reextract_all_predictions(chunk_size: int = 100) -> dict[str, Any]:
    """Run the new extractor over existing predictions and stage the results.

    The output is written to `predictions_reextracted` (not `predictions`)
    so the admin can compare — see db.reextraction_diff_summary and
    db.apply_reextraction_switchover. On chunk-by-chunk failure the job
    returns what it did so the admin sees partial progress.
    """
    import db
    from intelligence.prediction_extractor import extract_predictions_from_post

    with db.conn() as c:
        rows = c.execute(
            """
            SELECT p.*
            FROM predictions p
            LEFT JOIN predictions_reextracted r
                ON r.original_prediction_id = p.id
            WHERE r.id IS NULL
            ORDER BY p.id ASC
            LIMIT ?
            """,
            (int(chunk_size),),
        ).fetchall()

    processed = 0
    diffs = 0
    for r in rows:
        payload = await extract_predictions_from_post({
            "post_id": str(r["id"]),
            "content": r["content"],
            "author_handle": r["source_handle"],
        })
        new_direction = (payload.get("direction") or "").upper() or None
        new_category = payload.get("category") or r["category"]
        new_prob = payload.get("explicit_probability")
        matches = (
            new_direction == r["direction"]
            and new_category == r["category"]
            and (new_prob or 0) == (r["predicted_probability"] or 0)
        )
        diff_summary_parts = []
        if new_direction != r["direction"]:
            diff_summary_parts.append(f"direction: {r['direction']} → {new_direction}")
        if new_category != r["category"]:
            diff_summary_parts.append(f"category: {r['category']} → {new_category}")
        if (new_prob or 0) != (r["predicted_probability"] or 0):
            diff_summary_parts.append(
                f"probability: {r['predicted_probability']} → {new_prob}"
            )
        diff_summary = "; ".join(diff_summary_parts) if diff_summary_parts else None

        db.insert_reextracted_prediction({
            "original_prediction_id": r["id"],
            "source_handle": r["source_handle"],
            "market_id": r["market_id"],
            "category": new_category,
            "direction": new_direction,
            "predicted_probability": new_prob,
            "content": r["content"],
            "source_url": r["source_url"],
            "extracted_at": int(time.time()),
            "claim": payload.get("claim"),
            "explicit_probability": payload.get("explicit_probability"),
            "implicit_confidence": payload.get("implicit_confidence"),
            "time_frame": payload.get("time_frame"),
            "contains_sarcasm": payload.get("contains_sarcasm"),
            "is_conditional": payload.get("is_conditional"),
            "matches_original": matches,
            "diff_summary": diff_summary,
        })
        processed += 1
        if not matches:
            diffs += 1

    return {"processed": processed, "diffs": diffs}


# ── 3. Market categorisation — fill cache in the background ─────────────────


@register_job("categorise_uncached_markets")
async def categorise_uncached_markets_job(limit: int = CATEGORISATION_CHUNK_SIZE) -> dict[str, Any]:
    """Pick up to *limit* markets missing a categorisation row and fill the
    cache. The categoriser itself handles Claude errors and logs usage.
    """
    import db
    from backend.markets.unified_markets import fetch_unified_markets
    from backend.markets.polymarket_client import PolymarketClient
    from backend.markets.kalshi_client import KalshiClient
    from intelligence.categoriser import categorise_market

    try:
        markets = await fetch_unified_markets(PolymarketClient(), KalshiClient())
    except Exception as exc:
        log.warning("categoriser: market fetch failed: %s", exc)
        return {"error": "market fetch failed", "detail": str(exc)}

    ids = [m.id for m in markets]
    to_do = db.list_uncategorised_market_ids(ids)[: int(limit)]
    id_to_market = {m.id: m for m in markets}

    categorised = 0
    for mid in to_do:
        m = id_to_market.get(mid)
        if m is None:
            continue
        try:
            await categorise_market(m)
            categorised += 1
        except Exception as exc:
            log.warning("categoriser: failed for %s: %s", mid, exc)

    return {"considered": len(ids), "categorised": categorised}


# ── 4. Source summary — monthly regeneration ─────────────────────────────────


@register_job("regenerate_stale_source_summaries")
async def regenerate_stale_source_summaries_job(limit: int = SUMMARY_CHUNK_SIZE) -> dict[str, Any]:
    """Refresh summaries whose cache has expired (or was never generated).

    The summariser caches both Claude-generated and fallback summaries for
    30 days, so this job naturally paces itself — only expired rows come
    back from list_stale_source_summaries.
    """
    import db
    from intelligence.source_summary import generate_source_summary

    rows = db.list_stale_source_summaries(limit=int(limit))
    generated = 0
    for r in rows:
        try:
            await generate_source_summary(r["source_handle"])
            generated += 1
        except Exception as exc:
            log.warning("source_summary: failed for %s: %s", r["source_handle"], exc)

    return {"considered": len(rows), "generated": generated}


# Daily Claude spend alert moved to jobs/claude_cost_check.py - see
# module-top note. Fix C: removed the duplicate @register_job since the
# duplicate guard in jobs.registry.register_job now rejects two
# registrations under the same name.


# ── Cron schedules ───────────────────────────────────────────────────────────

# Extraction keeps up with the scraper without waiting for the next pipeline.
register_cron("run_extract_for_recent_posts", minute=5)
register_cron("run_extract_for_recent_posts", minute=25)
register_cron("run_extract_for_recent_posts", minute=45)

# Market categorisation trickles in — 50 markets/hour caps spend regardless
# of how many new markets the upstream APIs expose in a given day.
register_cron("categorise_uncached_markets", minute=13)

# Source summaries: once a day, not monthly — the summariser's own 30-day
# cache keeps actual Claude spend monthly-ish per source, while this
# cadence lets newly-rated sources pick up a summary quickly.
register_cron("regenerate_stale_source_summaries", hour=4, minute=30)

# Daily spend alert cron lives in jobs/claude_cost_check.py (Fix C).
# Registering it here would now point at a function this module no
# longer defines.
