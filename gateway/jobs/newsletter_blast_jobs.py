"""Newsletter blast tick — drain the deferred tail of a bounded send.

Background:
  ``/admin/newsletter/send`` (gateway/admin_routes.py::newsletter_send)
  caps the synchronous fan-out at ``MAX_INLINE_RECIPIENTS`` recipients
  per POST. Anything past that is recorded as a row in
  ``newsletter_blast_jobs`` (migration 187). This cron job picks up the
  oldest pending/running row and enqueues the next
  ``MAX_BATCH_PER_TICK`` recipients per tick, advancing the cursor
  until the row is fully drained.

Schedule:
  Every minute (``register_cron`` with all fields ``None``). With a
  500-row batch that's 30k recipients/hour for a single deferred blast
  — fast enough that a 100k blast finishes in under four hours, slow
  enough that the worker pool isn't starved by a single tail.

Race-safety (AUDIT 2026-05-15, migration 194):
  * ``claim_blast_job`` does an atomic ``UPDATE ... RETURNING`` with a
    per-worker claim token. Two scheduler instances calling the same
    tick concurrently can no longer both win the same row — exactly
    one returns the row, the other returns None and idles.
  * Pagination switched from LIMIT/OFFSET to cursor (``WHERE id >
    last_recipient_id``). An unsubscribe between ticks no longer
    causes a re-send or skip; the cursor is stable across any
    concurrent mutation of ``newsletter_subscribers``.
  * Progress + cursor + claim release are bumped in one UPDATE in
    ``advance_blast_job_progress_with_cursor``. A crashed worker's
    claim is reclaimable after ``CLAIM_TTL_SECONDS`` so a real crash
    unblocks the queue inside the same admin polling interval.

Crash-safety:
  * If the tick raises after the atomic claim, the row stays
    ``running`` with a stale ``claim_token``. After CLAIM_TTL the next
    tick re-claims it and resumes from ``last_recipient_id``.
  * If a recipient's ``enqueue_email`` fails, we log + advance — losing
    one row is preferable to wedging the whole tail.
  * If the live recipient set shrinks below the recorded
    ``total_recipients`` (e.g. mass-unsubscribe between sends), the
    cursor query returns no rows and we mark the job done.
"""

from __future__ import annotations

import logging
import secrets
import socket
import time
from typing import Any

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.newsletter_blast_jobs")


def _claim_token() -> str:
    """Per-process claim token. host:pid:rand keeps it human-readable in
    the DB while still being globally unique enough to make a
    cross-host collision astronomically unlikely.
    """
    try:
        host = socket.gethostname() or "host"
    except Exception:
        host = "host"
    import os as _os
    return f"{host}:{_os.getpid()}:{secrets.token_urlsafe(6)}"


@register_job("newsletter_blast_tick")
async def newsletter_blast_tick() -> dict[str, Any]:
    """Drain one batch of the oldest deferred blast tail.

    Returns a structured payload so the /admin/jobs audit row shows
    impact at a glance: ``{job_id, campaign_id, batch_size,
    processed_after, total, status_after}``.
    """
    import db

    # Atomic claim — at most one worker wins this row per tick.
    claim_token = _claim_token()
    job = db.claim_blast_job(claim_token)
    if job is None:
        return {"status": "idle"}

    job_id = int(job["id"])
    campaign_id = int(job["campaign_id"])
    total = int(job["total_recipients"])
    processed = int(job["processed_recipients"])
    last_recipient_id = int(job.get("last_recipient_id") or 0)

    # Look up the campaign to re-derive the recipient query. We do not
    # store the segment/frequency on the job row because the campaign
    # row is the source of truth — duplicating the filter would risk
    # drift on a backfill / replay.
    with db.conn() as c:
        camp_row = c.execute(
            "SELECT id, subject, body_md, segment, frequency_filter "
            "FROM newsletter_campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
    if camp_row is None:
        log.warning(
            "newsletter_blast_tick: campaign %d for job %d missing — "
            "marking failed", campaign_id, job_id,
        )
        db.mark_blast_job_failed(job_id)
        return {
            "job_id": job_id,
            "campaign_id": campaign_id,
            "status_after": "failed",
            "reason": "campaign_missing",
        }

    remaining = max(0, total - processed)
    if remaining <= 0:
        # Defensive: a previous tick already drained the tail but didn't
        # close the row. Close it now via the cursor-aware bump (with
        # batch_size=0 the cursor stays put but the status flip runs).
        db.advance_blast_job_progress_with_cursor(
            job_id,
            batch_size=0,
            last_recipient_id=last_recipient_id,
            claim_token=claim_token,
        )
        _maybe_backfill_sent_at(db, campaign_id, job_id)
        return {
            "job_id": job_id,
            "campaign_id": campaign_id,
            "status_after": "done",
            "batch_size": 0,
            "processed_after": processed,
            "total": total,
        }

    batch_cap = int(db.NEWSLETTER_MAX_BATCH_PER_TICK)
    batch_size = min(batch_cap, remaining)

    try:
        rows = db.get_blast_recipients_after(
            segment=camp_row["segment"],
            frequency_filter=camp_row["frequency_filter"],
            last_id=last_recipient_id,
            limit=batch_size,
        )
    except Exception as exc:
        log.exception(
            "newsletter_blast_tick: page fetch failed (job=%d): %s",
            job_id, exc,
        )
        db.mark_blast_job_failed(job_id)
        return {
            "job_id": job_id,
            "campaign_id": campaign_id,
            "status_after": "failed",
            "reason": "page_fetch_error",
        }

    if not rows:
        # Recipient set shrank below what we expected (mass unsubscribe
        # between handler and tick), or the cursor has moved past every
        # remaining subscriber. Either way, the tail is done — close it
        # and let advance_with_cursor flip status to done.
        log.info(
            "newsletter_blast_tick: job %d found no recipients past "
            "id=%d (live count drift) — closing", job_id, last_recipient_id,
        )
        db.advance_blast_job_progress_with_cursor(
            job_id,
            batch_size=remaining,
            last_recipient_id=last_recipient_id,
            claim_token=claim_token,
        )
        _maybe_backfill_sent_at(db, campaign_id, job_id)
        return {
            "job_id": job_id,
            "campaign_id": campaign_id,
            "status_after": "done",
            "batch_size": 0,
            "processed_after": processed + remaining,
            "total": total,
            "reason": "no_more_recipients",
        }

    # Resolve the renderer lazily; the markdown→HTML pass lives on
    # admin_routes but is harmless to call from a worker context. We
    # render once per batch (every recipient gets the same body).
    from admin_routes import _newsletter_md_to_html
    from jobs.email_jobs import enqueue_email

    body_html_str = _newsletter_md_to_html(camp_row["body_md"])
    subject = (camp_row["subject"] or "").strip()
    segment = camp_row["segment"]

    enqueued = 0
    max_id_in_batch = last_recipient_id
    for row in rows:
        try:
            await enqueue_email(
                to=row["email"],
                template="newsletter_blast",
                context={
                    "subject": subject,
                    "raw_body_html": body_html_str,
                },
                tags=["newsletter_blast", f"segment:{segment}"],
            )
            enqueued += 1
        except Exception as exc:
            log.warning(
                "newsletter_blast_tick: enqueue failed for %s (job=%d): %s",
                row["email"], job_id, exc,
            )
        # Bump the cursor by the row's id whether or not enqueue
        # succeeded. The recipient was visible at this offset in the
        # cursor pass; a retry on the next tick would re-send.
        if int(row["id"]) > max_id_in_batch:
            max_id_in_batch = int(row["id"])

    # Bump processed + cursor + release the claim in one atomic UPDATE.
    # The claim_token clause means a crashed-then-reclaimed worker can't
    # clobber the surviving worker's progress.
    updated = db.advance_blast_job_progress_with_cursor(
        job_id,
        batch_size=len(rows),
        last_recipient_id=max_id_in_batch,
        claim_token=claim_token,
    )
    status_after = updated.get("status") if isinstance(updated, dict) else None

    if status_after == "done":
        _maybe_backfill_sent_at(db, campaign_id, job_id)

    return {
        "job_id": job_id,
        "campaign_id": campaign_id,
        "batch_size": len(rows),
        "enqueued": enqueued,
        "last_recipient_id": max_id_in_batch,
        "processed_after": int(updated.get("processed_recipients") or 0)
        if isinstance(updated, dict) else processed + len(rows),
        "total": total,
        "status_after": status_after or "running",
    }


def _maybe_backfill_sent_at(db_mod, campaign_id: int, job_id: int) -> None:
    """Stamp ``newsletter_campaigns.sent_at`` once the tail closes.

    Split into a helper because every branch that flips the job to
    ``done`` needs the same backfill. Idempotent — the underlying
    UPDATE no-ops if ``sent_at`` is already set.
    """
    try:
        db_mod.backfill_campaign_sent_at(campaign_id, int(time.time()))
    except Exception as exc:
        log.warning(
            "newsletter_blast_tick: sent_at backfill failed "
            "(campaign=%d, job=%d): %s",
            campaign_id, job_id, exc,
        )


# Every minute. ``weekday`` / ``day`` / ``hour`` / ``minute`` all None
# means "fire on every cron tick" — the scheduler's adapter
# (scheduler/registry.py::_cron_from_legacy) translates that to
# ``* * * * *``.
register_cron("newsletter_blast_tick")
