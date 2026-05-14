"""Outbound webhook hardening — dead-letter queue + circuit-breaker fields.

Adds:

  ``webhook_dead_letter`` — one row per (subscription, event) tuple that
  burned through every retry without a 2xx. The admin panel reads from
  here to surface "stuck" deliveries the owner can inspect or re-queue.
  Payload is stored verbatim so re-queue is a straight POST without any
  schema-versioning gymnastics. ``ON DELETE CASCADE`` on the FK so a
  user nuking a subscription doesn't leave orphan DLQ rows behind.

  ``webhook_subscriptions.disabled_until`` — UNIX seconds. When the
  circuit breaker fires (10 consecutive failures in the new code path),
  we stamp this with ``now + 1h`` rather than hard-disabling the row.
  The delivery loop refuses to send while ``disabled_until > now``;
  once the clock rolls past, the breaker self-heals. This lets a
  flapping subscriber recover automatically — important because the
  default ``is_active = 0`` path requires user action to flip back on.

  ``webhook_subscriptions.consecutive_failures`` — already exists from
  migration 129. The ALTER is gated on ``PRAGMA table_info`` so re-runs
  on instances built before 129 still work without spurious errors.

Additive only. Safe to re-run.
"""

from __future__ import annotations


revision = "179"
down_revision = "178"


def _cols(c, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS webhook_dead_letter (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id   INTEGER NOT NULL REFERENCES webhook_subscriptions(id) ON DELETE CASCADE,
            event_type        TEXT NOT NULL,
            payload           TEXT NOT NULL,
            last_error        TEXT,
            attempts          INTEGER NOT NULL,
            first_failed_at   INTEGER NOT NULL,
            last_attempt_at   INTEGER NOT NULL,
            requeued_at       INTEGER
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_webhook_dlq_sub "
        "ON webhook_dead_letter(subscription_id, first_failed_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_webhook_dlq_open "
        "ON webhook_dead_letter(requeued_at) WHERE requeued_at IS NULL"
    )

    sub_cols = _cols(c, "webhook_subscriptions")
    # consecutive_failures was added in 129, but older clones may not have it.
    if "consecutive_failures" not in sub_cols:
        c.execute(
            "ALTER TABLE webhook_subscriptions "
            "ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0"
        )
    if "disabled_until" not in sub_cols:
        c.execute(
            "ALTER TABLE webhook_subscriptions "
            "ADD COLUMN disabled_until INTEGER"
        )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_webhook_dlq_open")
    c.execute("DROP INDEX IF EXISTS idx_webhook_dlq_sub")
    c.execute("DROP TABLE IF EXISTS webhook_dead_letter")
    # SQLite has no DROP COLUMN before 3.35 in the safe form; leave the
    # subscription columns in place on downgrade. They're nullable / default
    # zero so an older binary running against the post-179 schema is fine.
