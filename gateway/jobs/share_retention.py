"""Retention cron: prune long-expired shared_* rows.

Share tokens have a 7-day TTL (``share_tokens.DEFAULT_TTL_SECONDS``).
After expiry a row still hangs around in ``shared_market_cards`` /
``shared_source_cards`` / ``shared_predictions`` forever — harmless for
correctness (expired tokens 404 at read time) but the tables grow
linearly over years.

Delete rows whose ``expires_at`` is older than 30 days. The grace
window lets admins investigate recent expired shares (e.g. a sharer
claiming a card vanished too quickly) without digging through a backup.

``share_metrics`` rows are NOT pruned here. They're the historical
conversion record that /admin/sharing aggregates — losing them would
break month-over-month reporting. A separate retention pass can
summarise + aggregate them down the line; for now they stay.

Schedule: daily at 03:20 UTC. Offset from other 03:* jobs so the
SQLite write lock isn't contested with the leaderboard scorer or
reconcile jobs.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.share_retention")


# Tables to sweep. Keeping them in a tuple rather than computing from
# a schema introspection: if a future migration adds a fourth
# ``shared_*`` table, the retention policy needs an explicit review
# (does it have a view_count, an expires_at, a different TTL?), not a
# silent auto-include.
_SHARE_TABLES: tuple[str, ...] = (
    "shared_market_cards",
    "shared_source_cards",
    "shared_predictions",
)

# Grace window past expiry before a row is eligible for deletion.
# 30 days matches the data_exports retention grace (see
# migration 030) so admins investigating share issues have the same
# recovery window they do for other user-facing artefacts.
GRACE_SECONDS = 30 * 24 * 3600


@register_job("share_retention_prune")
async def share_retention_prune() -> dict[str, Any]:
    """Delete share rows whose expires_at is older than now - GRACE.

    Returns a per-table rowcount so /admin/jobs log shows impact
    without a follow-up query."""
    import db

    cutoff = int(time.time()) - GRACE_SECONDS
    deleted_by_table: dict[str, int] = {}
    total = 0

    for table in _SHARE_TABLES:
        try:
            with db.conn() as c:
                cur = c.execute(
                    # Table name from a whitelisted constant above —
                    # never user input — so literal interpolation is
                    # safe. The cutoff is parameterised.
                    f"DELETE FROM {table} WHERE expires_at < ?",
                    (cutoff,),
                )
                n = cur.rowcount if cur.rowcount is not None else 0
        except Exception:
            # Don't let one failing table block the others — a rare
            # schema drift (e.g. a migration renamed expires_at mid-
            # flight) should produce a warning per table and keep the
            # others healthy.
            log.exception("share_retention: failed on %s", table)
            n = 0
        deleted_by_table[table] = n
        total += n

    log.info(
        "share_retention: cutoff=%d total_deleted=%d by_table=%s",
        cutoff, total, deleted_by_table,
    )
    return {
        "cutoff_unix": cutoff,
        "total_deleted": total,
        "by_table": deleted_by_table,
    }


# 03:20 UTC daily. Offset from leaderboard scorer (03:00) + reconcile
# subscriptions (03:17) + any other 03:* nightly jobs so the single
# SQLite writer isn't contested.
register_cron("share_retention_prune", hour=3, minute=20)
