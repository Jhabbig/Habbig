"""Backfill ``subscriptions.expires_at`` for legacy rows.

Audit finding (MED-1, queries/billing)
--------------------------------------
The Stripe webhook handlers (``_grant_access`` / ``_update_plan`` /
``_record_payment``) historically forgot to write
``subscriptions.expires_at``, leaving the column NULL on every
Stripe-sourced row. ``has_active_subscription`` then treated NULL as
"no expiry" → those users kept paid access forever, even after a
missed ``customer.subscription.deleted`` event.

The webhook now always writes ``expires_at = current_period_end`` and
``has_active_subscription`` fails closed on NULL. This migration
repairs the legacy rows: any active Stripe-sourced row with NULL
``expires_at`` gets its window stamped to NOW so the next renewal pass
re-syncs it from Stripe. Setting it to NOW (rather than infinity)
keeps the closed-fail invariant — a row whose webhook never landed
will read as expired until the next Stripe event refreshes it.

Manual upserts (CLI grants, gift flows) that legitimately set NULL
``expires_at`` (no duration_days) are NOT repaired — those rows
represent permanent grants and should continue to fail closed under
the new rule. The admin tooling that creates them is the right place
to set a non-NULL expiry going forward.

Idempotent: re-running is a no-op because the WHERE clause matches
only NULL rows and a backfilled run leaves no NULLs to touch.
"""

from __future__ import annotations

import sqlite3
import time


revision = "193"
down_revision = "192"


def _table_exists(c: sqlite3.Connection, name: str) -> bool:
    row = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def upgrade(c: sqlite3.Connection) -> None:
    # subscriptions is created by 001_initial_schema. Guard for fresh DBs
    # in case the migration is run against a partial bootstrap.
    if not _table_exists(c, "subscriptions"):
        return
    now = int(time.time())
    # Stamp NULL expires_at on Stripe-sourced active rows to NOW. This
    # forces the closed-fail rule to bite immediately for orphaned rows
    # whose Stripe webhook never landed — the next subscription.updated
    # or invoice.paid event from Stripe will refresh expires_at to the
    # real current_period_end.
    c.execute(
        "UPDATE subscriptions "
        "SET expires_at = ? "
        "WHERE expires_at IS NULL "
        "AND source = 'stripe' "
        "AND status = 'active'",
        (now,),
    )


def downgrade(c: sqlite3.Connection) -> None:
    # Deliberately a no-op. Re-introducing NULL ``expires_at`` would
    # restore the open-fail vulnerability the backfill exists to plug;
    # a rollback that keeps the column populated is the safe direction.
    pass
