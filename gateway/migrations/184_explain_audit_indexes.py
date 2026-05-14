"""Indexes for query patterns surfaced by EXPLAIN audit (2026-05-14).

Audit method: built a clean SQLite at /tmp/narve_audit.db, ran every
migration to head (102 applied), then drove the ~30 hottest
queries through ``EXPLAIN QUERY PLAN`` with realistic parameters. The
five indexes below remove a ``SCAN <table>`` or ``USE TEMP B-TREE FOR
ORDER BY`` step from a measured-frequent query:

  * ``subscriptions(status, dashboard_key, expires_at)`` — covers every
    admin billing aggregate (``count_active_subscribers``,
    ``get_active_subscription_counts_by_dashboard``,
    ``get_revenue_stats`` cancelled count). Today these full-scan the
    subscriptions table on every /admin/subproducts and /admin/revenue
    render. Composite ordering matches the WHERE-clause prefix so a
    single index serves all three queries.

  * ``subscriptions(started_at)`` — used by ``get_new_signups`` and
    ``get_signups_daily_series`` for the admin signups sparkline. These
    full-scan today; with the index the date-range slice becomes a
    bounded index walk.

  * ``audit_log(action, id DESC)`` — the v2 audit-log search
    (``queries/audit.py``) cursor-paginates with ``ORDER BY id DESC``.
    The existing ``idx_audit_action`` indexes ``(action, timestamp)``
    so an action-filter query still has to temp-btree-sort by id.
    Indexing on ``id DESC`` lets the planner walk in cursor order.

  * ``audit_log(target_id, id DESC)`` — searching by target_user_id
    today is a full table scan because ``idx_audit_target`` leads with
    ``target_type``, not ``target_id``. This new index serves the
    common "show me all events for user 1234" filter without
    requiring the admin to also pick a target_type.

  * ``gifted_subscriptions(created_at DESC) WHERE revoked = 0`` —
    partial index for ``list_active_gifts``. The current
    ``idx_gifts_active(revoked, ends_at)`` filters fine but leaves the
    ``ORDER BY created_at DESC`` to a temp btree. The planner won't
    always pick the partial index on a tiny table — it kicks in as
    soon as the gifts list grows past the planner's threshold,
    avoiding a regression when admin gifts scale up.

Every index uses IF NOT EXISTS. Additive-only; downgrade drops them all.
"""

from __future__ import annotations

import sqlite3


revision = "184"
down_revision = "183"


_INDEXES: list[tuple[str, str]] = [
    (
        "idx_subs_status_dashboard",
        "CREATE INDEX IF NOT EXISTS idx_subs_status_dashboard "
        "ON subscriptions(status, dashboard_key, expires_at)",
    ),
    (
        "idx_subs_started",
        "CREATE INDEX IF NOT EXISTS idx_subs_started "
        "ON subscriptions(started_at)",
    ),
    (
        "idx_audit_action_id",
        "CREATE INDEX IF NOT EXISTS idx_audit_action_id "
        "ON audit_log(action, id DESC)",
    ),
    (
        "idx_audit_target_id",
        "CREATE INDEX IF NOT EXISTS idx_audit_target_id "
        "ON audit_log(target_id, id DESC)",
    ),
    (
        "idx_gifts_active_created",
        "CREATE INDEX IF NOT EXISTS idx_gifts_active_created "
        "ON gifted_subscriptions(created_at DESC) WHERE revoked = 0",
    ),
]


def upgrade(c: sqlite3.Connection) -> None:
    for _name, ddl in _INDEXES:
        c.execute(ddl)


def downgrade(c: sqlite3.Connection) -> None:
    for name, _ddl in _INDEXES:
        c.execute(f"DROP INDEX IF EXISTS {name}")
