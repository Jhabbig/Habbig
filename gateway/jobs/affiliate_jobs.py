"""Daily commission-calc job for the private affiliate program.

Walks every conversion that has a first payment recorded but no
commission calculated yet, multiplies first_payment × commission_rate,
stores the result and keeps the account-level total in sync.

When a conversion tips an affiliate's pending balance across the £50
threshold for the first time, queues an email nudging them to request
a payout from the dashboard.

Scheduled daily at 02:00 UTC — low traffic hour, well after Stripe's
webhook bursts.

Gotcha: the Stripe webhook that populates ``first_payment_amount_pence``
isn't wired yet (see server_features.py TODO). Until it is, this job is
a safe no-op — ``list_conversions_awaiting_commission_calc`` returns
empty, we log that, we return. No fallback "guess the payment amount"
logic is ever desirable here; unknown amounts stay unprocessed.
"""

from __future__ import annotations

import logging
from typing import Any

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.affiliate")


_DEFAULT_BATCH_SIZE = 200


@register_job("calculate_affiliate_commissions")
async def calculate_affiliate_commissions(
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """For each paid conversion without a commission, compute it.

    Strategy is batched so a burst of payments doesn't blow memory or
    hold the SQLite write lock for an unbounded time. If the batch size
    is saturated, the job re-enqueues itself to drain the rest.

    Emails a threshold-hit nudge on the commission that FIRST crosses
    the affiliate's pending balance over £50. Determined stateless-ly
    by comparing pending_before (= pending_after − just_recorded_amount)
    against the threshold.
    """
    import db_affiliate as da

    rows = da.list_conversions_awaiting_commission_calc(limit=batch_size)
    if not rows:
        log.info("affiliate: no conversions awaiting commission calc")
        return {"processed": 0, "emailed": 0, "more": False}

    processed = 0
    emailed = 0
    threshold = da.DEFAULT_PAYOUT_THRESHOLD_PENCE
    # Track per-affiliate so we don't email twice in one batch if two
    # conversions arrive together and both straddle the threshold.
    emailed_affiliate_ids: set[int] = set()

    for row in rows:
        conv_id = row["id"]
        affiliate_id = row["affiliate_account_id"]
        rate = float(row["commission_rate"])
        first_payment = int(row["first_payment_amount_pence"] or 0)
        if first_payment <= 0:
            # Defensive: row shouldn't be in the awaiting list with zero,
            # but guard against corrupt Stripe webhook payload.
            log.warning(
                "affiliate: conv id=%d has first_payment=0, skipping",
                conv_id,
            )
            continue

        commission_pence = int(round(first_payment * rate))
        # Sanity floor: even a 5% rate on a £1 subscription yields 5p.
        # Stripe rounds to whole pence too so this is enough.
        if commission_pence <= 0:
            commission_pence = 1

        ok = da.record_commission_calculated(conv_id, commission_pence)
        if not ok:
            # Someone else already calculated this row (re-queued job
            # racing a manual fix). Move on.
            log.info(
                "affiliate: conv id=%d already calculated, skipping",
                conv_id,
            )
            continue
        processed += 1
        log.info(
            "affiliate: conv %d → £%.2f × %.2f = £%.2f",
            conv_id, first_payment / 100, rate, commission_pence / 100,
        )

        # Threshold nudge — only on the row that actually crosses.
        if affiliate_id in emailed_affiliate_ids:
            continue

        summary = da.sum_affiliate_commissions(affiliate_id)
        pending_after = summary["pending_pence"]
        pending_before = pending_after - commission_pence
        if pending_before < threshold <= pending_after:
            try:
                await _enqueue_threshold_email(affiliate_id, pending_after)
                emailed += 1
                emailed_affiliate_ids.add(affiliate_id)
            except Exception:
                log.exception(
                    "affiliate: threshold email enqueue failed for id=%d",
                    affiliate_id,
                )

    # If we filled the batch, there may be more rows. Re-enqueue.
    more = len(rows) >= batch_size
    if more:
        try:
            from jobs import enqueue_job
            await enqueue_job("calculate_affiliate_commissions", batch_size=batch_size)
        except Exception:
            log.exception("affiliate: re-enqueue of commission calc failed")

    return {"processed": processed, "emailed": emailed, "more": more}


async def _enqueue_threshold_email(affiliate_id: int, pending_pence: int) -> None:
    """Queue the ``affiliate_payout_threshold`` email for the owner of
    ``affiliate_id``. Uses the existing ``send_email`` job so delivery,
    retries, and audit logging are uniform with every other email.
    """
    import db
    import db_affiliate as da
    from jobs.email_jobs import enqueue_email

    aff = da.get_affiliate_by_id(affiliate_id)
    if not aff:
        return
    user = db.get_user_by_id(aff["user_id"])
    if not user:
        return
    to_addr = aff["payout_email"] or user["email"]
    if not to_addr:
        log.warning("affiliate: no email on file for affiliate_id=%d", affiliate_id)
        return

    await enqueue_email(
        to=to_addr,
        template="affiliate_payout_threshold",
        context={
            "display_name": user["username"] or user["email"],
            "pending_gbp": f"{pending_pence / 100:.2f}",
            "threshold_gbp": f"{da.DEFAULT_PAYOUT_THRESHOLD_PENCE / 100:.0f}",
            "dashboard_url": "https://narve.ai/settings/affiliate",
        },
        tags=["affiliate", "payout_threshold"],
    )
    log.info(
        "affiliate: threshold email queued for affiliate_id=%d pending=£%.2f",
        affiliate_id, pending_pence / 100,
    )


# Schedule: 02:10 UTC daily. Offset from the top-of-hour so we don't
# collide with the other 02:00 nightly jobs (digests, exports).
register_cron(
    "calculate_affiliate_commissions",
    hour=2,
    minute=10,
)
