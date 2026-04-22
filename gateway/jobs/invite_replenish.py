"""Monthly per-tier invite-token replenishment.

Runs once on the 1st of every month at 00:05 UTC. Walks every active
paid subscriber and grants their tier's monthly allotment, capped at
2× the allotment (enforced inside ``db_sharing.replenish_invites_for_user``).

Why a background job and not inline-on-subscription-renew:

  * Stripe invoice timing drifts — a monthly subscriber whose billing
    cycle starts on the 12th shouldn't wait 12 days for the 1st's
    allotment.
  * Mid-month tier upgrades (Trader → Pro) should still result in 5
    invites next month, not a mix of 2 + 3.
  * Idempotency: the ``invites_replenished_yyyymm`` guard on the user
    row means a retry (e.g. scheduler crash mid-batch) doesn't double
    grant. See db_sharing.replenish_invites_for_user for the exact
    check.

Cron pattern: APScheduler's ``minute=5, hour=0, day=1`` fires on the
first day of every month. The legacy ``register_cron`` decorator in
use across ``jobs/`` only takes ``hour`` and ``minute`` — it runs
daily, so we gate on ``datetime.utcnow().day == 1`` inside the job
body as a second line of defence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.invite_replenish")


def _current_yyyymm() -> int:
    """YYYYMM as an int so the DB guard is a plain integer compare, not
    a string parse. 202604 sorts naturally: a later month's integer is
    always larger."""
    now = datetime.now(timezone.utc)
    return now.year * 100 + now.month


@register_job("replenish_invites")
async def replenish_invites() -> dict[str, Any]:
    """Grant every active paid subscriber their monthly invite allotment.

    Returns a summary dict so the /admin/jobs log shows reach + impact
    without a follow-up query. Fields:
      * eligible_users: paid users we considered
      * granted: new tokens minted this run
      * pruned: old unused tokens revoked to keep under 2× cap
      * skipped_already_replenished: users the idempotency guard caught
      * not_first_of_month: returned early because it's a daily cron
        firing on a non-1st
    """
    now = datetime.now(timezone.utc)
    if now.day != 1:
        # Day-of-month guard. Kept inside the job so a future switch to
        # a true monthly cron still works — the guard just becomes a
        # no-op then.
        log.info(
            "invite_replenish: not 1st of month (day=%d), skipping",
            now.day,
        )
        return {
            "eligible_users": 0,
            "granted": 0,
            "pruned": 0,
            "skipped_already_replenished": 0,
            "not_first_of_month": True,
        }

    import db
    import db_sharing

    yyyymm = _current_yyyymm()
    eligible = 0
    granted = 0
    pruned = 0
    skipped = 0

    # Walk every user with an active paid subscription. We derive tier
    # via the same helper the rest of the codebase uses
    # (db.get_user_subscription_tier) — that way if a tier-definition
    # change lands in db.py, this job sees it automatically.
    with db.conn() as c:
        user_rows = c.execute(
            "SELECT id FROM users "
            "WHERE COALESCE(is_deleted, 0) = 0 "
            "  AND COALESCE(suspended, 0) = 0"
        ).fetchall()

    for row in user_rows:
        uid = row["id"]
        try:
            tier = db.get_user_subscription_tier(uid)
        except Exception:
            log.exception("invite_replenish: tier lookup failed for user %d", uid)
            continue
        if tier not in db_sharing.INVITE_ALLOTMENT_BY_TIER:
            # Free tier or unknown — no allotment.
            continue
        eligible += 1
        try:
            result = db_sharing.replenish_invites_for_user(
                user_id=uid, tier=tier, yyyymm=yyyymm,
            )
        except Exception:
            log.exception(
                "invite_replenish: replenish failed for user %d tier=%s",
                uid, tier,
            )
            continue
        if result["skipped"]:
            skipped += 1
        granted += result["granted"]
        pruned += result["pruned"]

    log.info(
        "invite_replenish: yyyymm=%d eligible=%d granted=%d pruned=%d skipped=%d",
        yyyymm, eligible, granted, pruned, skipped,
    )
    return {
        "eligible_users": eligible,
        "granted": granted,
        "pruned": pruned,
        "skipped_already_replenished": skipped,
        "not_first_of_month": False,
        "yyyymm": yyyymm,
    }


# 00:05 UTC daily. The job's own day-of-month guard ensures it's a
# no-op on non-1st days — cheaper than a monthly cron that needs a
# custom APScheduler trigger. :05 offset avoids the midnight herd.
register_cron("replenish_invites", hour=0, minute=5)
