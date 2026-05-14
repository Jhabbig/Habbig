"""Newsletter blast tick — drain the deferred tail of a bounded send.

Background:
  ``/admin/newsletter/send`` (gateway/admin_routes.py::newsletter_send)
  caps the synchronous fan-out at ``MAX_INLINE_RECIPIENTS`` recipients
  per POST. Anything past that is recorded as a row in
  ``newsletter_blast_jobs`` (migration 187). This cron job picks up the
  oldest pending/running row and enqueues the next
  ``MAX_BATCH_PER_TICK`` recipients per tick, advancing
  ``processed_recipients`` until the row is fully drained.

Schedule:
  Every minute (``register_cron`` with all fields ``None``). With a
  500-row batch that's 30k recipients/hour for a single deferred blast
  — fast enough that a 100k blast finishes in under four hours, slow
  enough that the worker pool isn't starved by a single tail.

Crash-safety:
  * If the tick raises after marking a row ``running``, the row stays
    ``running``. The next tick re-fetches it (``fetch_next_pending``
    matches both ``pending`` and ``running``) and resumes from
    ``processed_recipients``.
  * If a recipient's ``enqueue_email`` fails, we log + advance — losing
    one row is preferable to wedging the whole tail.
  * If ``count_blast_recipients`` drops below the recorded
    ``total_recipients`` (e.g. mass-unsubscribe between sends), the
    tick clamps the offset to the live count and marks the row done.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.newsletter_blast_jobs")


@register_job("newsletter_blast_tick")
async def newsletter_blast_tick() -> dict[str, Any]:
    """Drain one batch of the oldest deferred blast tail.

    Returns a structured payload so the /admin/jobs audit row shows
    impact at a glance: ``{job_id, campaign_id, batch_size,
    processed_after, total, status_after}``.
    """
    import db

    job = db.fetch_next_pending_blast_job()
    if job is None:
        return {"status": "idle"}

    # sqlite3.Row has no ``.get()`` — bracket-only access throughout.
    job_id = int(job["id"])
    campaign_id = int(job["campaign_id"])
    total = int(job["total_recipients"])
    processed = int(job["processed_recipients"])

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

    db.mark_blast_job_started(job_id)

    remaining = max(0, total - processed)
    if remaining <= 0:
        # Defensive: a previous tick already drained the tail but didn't
        # close the row. Close it now.
        db.advance_blast_job_progress(job_id, 0)
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

    # Page offset = how many recipients earlier passes have already
    # enqueued, INCLUDING the inline portion the request handler ran.
    # The inline portion equals (recipient_count - total_recipients).
    # We re-read recipient_count from the live table — if subscribers
    # unsubscribed in the meantime the offset still points past the
    # inline cap, which is fine: the recipient page just returns
    # fewer rows and the job finishes early.
    inline_count = (
        db.count_blast_recipients(
            segment=camp_row["segment"],
            frequency_filter=camp_row["frequency_filter"],
        )
        - total
    )
    offset = max(0, inline_count) + processed

    try:
        rows = db.get_blast_recipients_page(
            segment=camp_row["segment"],
            frequency_filter=camp_row["frequency_filter"],
            offset=offset,
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
        # The recipient table shrank below what we expected (mass
        # unsubscribe between handler and tick). Mark the job done so
        # we don't loop forever on a tail that no longer exists.
        log.info(
            "newsletter_blast_tick: job %d found no recipients at "
            "offset=%d (live count drift) — closing", job_id, offset,
        )
        db.advance_blast_job_progress(job_id, remaining)
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

    # We advance ``processed_recipients`` by the batch size we ATTEMPTED
    # (not just the ones that succeeded). A failed enqueue is logged but
    # the worker should move on — re-trying the same recipient on the
    # next tick would deadlock progress if e.g. the email pipeline is
    # rejecting them outright. The audit log captures the misses.
    updated = db.advance_blast_job_progress(job_id, len(rows))
    status_after = updated.get("status") if isinstance(updated, dict) else None

    if status_after == "done":
        _maybe_backfill_sent_at(db, campaign_id, job_id)

    return {
        "job_id": job_id,
        "campaign_id": campaign_id,
        "batch_size": len(rows),
        "enqueued": enqueued,
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
