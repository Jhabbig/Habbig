"""Newsletter blast jobs — deferred recipient enqueue queue.

Background:
  The synchronous send path on ``/admin/newsletter/send`` used to iterate
  every confirmed subscriber inside the request handler. With ~100k+
  subscribers that turns a single admin POST into 100k DB writes on the
  hot request path, easily stalling the worker for minutes.

This table backs the deferred-tail half of a bounded blast. The handler
enqueues up to ``MAX_INLINE_RECIPIENTS`` (~500) recipients inline and
records the remainder as a "pending" row here. A scheduler tick
(``newsletter_blast_tick``) pulls one pending row per tick, walks the
next batch of recipients, calls ``enqueue_email`` for each, and
advances ``processed_recipients``. Once ``processed == total`` the row
is marked ``done`` and the campaign's ``sent_at`` gets backfilled.

Columns:

  * ``id``                    INTEGER PRIMARY KEY.
  * ``campaign_id``           INTEGER NOT NULL — points back at
                              ``newsletter_campaigns.id``. Joined for
                              admin-UI render; we don't FK so a campaign
                              delete doesn't cascade (campaigns are
                              admin-authored history).
  * ``status``                TEXT NOT NULL — one of ``pending`` /
                              ``running`` / ``done`` / ``failed``.
  * ``total_recipients``      INTEGER NOT NULL — how many recipients
                              the deferred tail must process.
  * ``processed_recipients``  INTEGER NOT NULL DEFAULT 0 — how many of
                              ``total_recipients`` the tick worker has
                              already enqueued. ``processed == total``
                              flips status to ``done``.
  * ``created_at``            INTEGER NOT NULL — row insert time.
  * ``started_at``            INTEGER — populated the first time the
                              tick worker picks the row up. NULL while
                              the row is still pending.
  * ``finished_at``           INTEGER — populated when the row leaves
                              ``running`` (either ``done`` or
                              ``failed``).

Index:
  ``idx_newsletter_blast_jobs_pending`` accelerates the tick worker's
  "fetch the next pending row" lookup, which runs every minute.
"""

from __future__ import annotations


revision = "187"
down_revision = "186"


def upgrade(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS newsletter_blast_jobs (
          id INTEGER PRIMARY KEY,
          campaign_id INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          total_recipients INTEGER NOT NULL,
          processed_recipients INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL,
          started_at INTEGER,
          finished_at INTEGER
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_newsletter_blast_jobs_pending "
        "ON newsletter_blast_jobs(status, id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_newsletter_blast_jobs_campaign "
        "ON newsletter_blast_jobs(campaign_id)"
    )


def downgrade(cur) -> None:
    cur.execute("DROP INDEX IF EXISTS idx_newsletter_blast_jobs_campaign")
    cur.execute("DROP INDEX IF EXISTS idx_newsletter_blast_jobs_pending")
    cur.execute("DROP TABLE IF EXISTS newsletter_blast_jobs")
