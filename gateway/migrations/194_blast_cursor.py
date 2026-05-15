"""Newsletter blast cursor — cursor pagination + atomic claim.

Background:
  ``newsletter_blast_jobs`` (migration 187) tracks deferred-tail blast
  rows. The tick worker (``newsletter_blast_tick``) drains them in
  batches. The original drain used ``LIMIT/OFFSET`` on the recipient
  query, which is racy: if a subscriber unsubscribes between ticks the
  OFFSET shifts and the worker either resends rows or skips them.

  Worse, two scheduler instances can race the same row — both call
  ``fetch_next_pending_blast_job``, both mark it ``running``, both
  enqueue the same batch. End result: duplicate emails, double-spend on
  the email provider quota, double-count on campaign analytics.

This migration adds the two columns the cursor-based redesign needs:

  * ``last_recipient_id`` — the id of the last recipient enqueued. The
    next tick page is ``WHERE id > last_recipient_id`` — stable across
    inserts and unsubscribes. Defaults to 0 (first page starts at id > 0).

  * ``claim_token`` — a per-tick claim token. The atomic ``UPDATE … SET
    status='running', claim_token=? WHERE status IN ('pending','running')
    AND (claim_token IS NULL OR claim_token = ? OR started_at < ?)
    RETURNING *`` ensures exactly one worker owns the row per tick.
    A stale claim (worker crashed mid-batch) is reclaimable after the
    ``CLAIM_TTL_SECONDS`` grace window.

Both columns are nullable / default-0 so the migration is additive and
back-compat with existing rows in flight.
"""

from __future__ import annotations


revision = "194"
down_revision = "193"


def upgrade(cur) -> None:
    cols = {row["name"] for row in cur.execute(
        "PRAGMA table_info(newsletter_blast_jobs)",
    )}
    if "last_recipient_id" not in cols:
        cur.execute(
            "ALTER TABLE newsletter_blast_jobs "
            "ADD COLUMN last_recipient_id INTEGER NOT NULL DEFAULT 0"
        )
    if "claim_token" not in cols:
        cur.execute(
            "ALTER TABLE newsletter_blast_jobs "
            "ADD COLUMN claim_token TEXT"
        )

    # Helpful index for the atomic claim — we filter on status AND
    # (claim_token IS NULL OR started_at < cutoff). The partial index
    # below covers the hot path where status is one of the two active
    # states; ``started_at`` is included so the planner can skip rows
    # without falling back to a table scan on a busy ticker.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_newsletter_blast_jobs_claim "
        "ON newsletter_blast_jobs(status, started_at) "
        "WHERE status IN ('pending', 'running')"
    )


def downgrade(cur) -> None:
    cur.execute("DROP INDEX IF EXISTS idx_newsletter_blast_jobs_claim")
    # SQLite ALTER TABLE DROP COLUMN landed in 3.35 — but the existing
    # downgrade pattern in this project is best-effort, so we ignore
    # the columns and let a future migration tidy them.
