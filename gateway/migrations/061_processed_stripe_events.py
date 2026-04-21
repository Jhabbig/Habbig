"""Stripe webhook idempotency ledger.

Stripe retries failed webhooks for up to 3 days; a flaky handler can see
the same ``evt_*`` ID multiple times. This table records every event we
*finished* processing so a replay is a no-op:

  1. Handler verifies signature + livemode.
  2. ``INSERT OR IGNORE`` the event_id.
  3. If the INSERT was a no-op (event_id already present), return
     ``{"status": "already_processed"}`` and skip.
  4. Otherwise run the branch, then ``UPDATE`` processed_at at the end.

Without this ledger, a customer.subscription.deleted replay would
revoke sessions twice, enqueue two cancellation emails, and flap the
embed_widgets is_active flag — all observable by the user.

``processed_at`` is populated when the handler *completes*; a row with
NULL processed_at represents a started-but-crashed attempt, which the
admin panel surfaces so we can investigate.
"""

from __future__ import annotations

import sqlite3


revision = "061"
down_revision = "060"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_stripe_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id      TEXT NOT NULL UNIQUE,
            event_type    TEXT NOT NULL,
            livemode      INTEGER NOT NULL DEFAULT 0,
            received_at   INTEGER NOT NULL,
            processed_at  INTEGER,
            error         TEXT
        )
        """
    )
    # Typical query is "have we already seen this event_id?" — satisfied
    # by the UNIQUE constraint's implicit index. Add a second index on
    # received_at so the admin panel can page recent events without a
    # sort.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_stripe_events_received "
        "ON processed_stripe_events(received_at DESC)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_stripe_events_received")
    c.execute("DROP TABLE IF EXISTS processed_stripe_events")
