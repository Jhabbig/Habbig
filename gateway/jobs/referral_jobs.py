"""Referral reward processor + leaderboard scorer.

Two jobs live here, both register their own cron schedule at import time:

  process_referral_rewards        — daily at 02:15 UTC
  compute_user_leaderboard_scores — daily at 03:00 UTC

Kept in one module because they share the same DB connection pattern and
are tightly related (the leaderboard's opt-in flow is itself part of the
"referral + leaderboard" feature pair). Split later if either grows.

Design note — why we don't grant rewards inline on conversion:

  If we granted the reward in the same request that converts the invitee
  to paid, a reward for "5 referrals → tier upgrade" might race with a
  concurrent conversion from another invitee and both would see total=4,
  both would trigger the count=5 reward, and we'd double-gift. Running
  this in a single daily batch — one process, no concurrency — gives us
  a natural serialization point without needing a distributed lock.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.referral")


def _app_url() -> str:
    return os.environ.get("APP_URL", "https://narve.ai")


@register_job("process_referral_rewards")
async def process_referral_rewards() -> dict[str, Any]:
    """Find converted referrals without a reward and grant them.

    For each pending referral:
      1. Skip if referrer is not currently paying (we don't reward users
         who have since cancelled — brief says "referrer is still in good
         standing").
      2. Count referrer's already-rewarded conversions; this one will be
         conversion number N+1.
      3. Look up reward for conversion N+1 via backend.referrals.
      4. If there's a reward: insert a gifted_subscriptions row at the
         resolved tier, stamp the referral row, increment the user's
         running credit total, and enqueue a congratulations email.
      5. If no reward at this count (2, 3, 4, 6, 7, 8, 9): still stamp
         `reward_granted=1` with type='none' so we don't reprocess the
         same row tomorrow.

    Idempotent: a crash mid-batch re-processes the remaining pending rows
    on the next run. The stamping step is atomic (UPDATE … WHERE
    reward_granted=0) so a re-run won't double-apply anything already done.
    """
    import db
    import db_referrals as dbr
    from backend import referrals as referral_logic

    pending = dbr.list_pending_reward_referrals(limit=500)
    if not pending:
        return {"processed": 0, "granted": 0, "skipped_no_payer": 0}

    # ── N+1 fix ──────────────────────────────────────────────────────────
    # Each iteration of the original loop did 5 round-trips per referrer:
    # tier (2 queries: admin probe + active-subs), reward-already-stamped
    # count, full user row for the email, and converted-referral count.
    # We batch all of these into one chunked query each (500 ids per
    # chunk, well under SQLITE_MAX_VARIABLE_NUMBER=999) keyed by the
    # unique referrer set, then maintain dynamic counts in Python so the
    # ordering semantics of the original per-row SELECT are preserved.
    referrer_ids = sorted({row["referrer_user_id"] for row in pending})
    admin_ids: set[int] = set()
    referrer_plans: dict[int, list[str]] = {}
    user_rows: dict[int, sqlite3.Row] = {}
    already_baseline: dict[int, int] = {}
    converted_baseline: dict[int, int] = {}
    if referrer_ids:
        with db.conn() as c:
            for i in range(0, len(referrer_ids), 500):
                chunk = referrer_ids[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                for r in c.execute(
                    f"SELECT id FROM users WHERE id IN ({placeholders}) AND is_admin = 1",
                    chunk,
                ).fetchall():
                    admin_ids.add(r["id"])
                for r in c.execute(
                    f"SELECT user_id, plan FROM subscriptions "
                    f"WHERE user_id IN ({placeholders}) AND status = 'active'",
                    chunk,
                ).fetchall():
                    referrer_plans.setdefault(r["user_id"], []).append(r["plan"] or "")
                for r in c.execute(
                    f"SELECT * FROM users WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall():
                    user_rows[r["id"]] = r
                for r in c.execute(
                    f"SELECT referrer_user_id, COUNT(*) AS n FROM referrals "
                    f"WHERE referrer_user_id IN ({placeholders}) AND reward_granted = 1 "
                    f"GROUP BY referrer_user_id",
                    chunk,
                ).fetchall():
                    already_baseline[r["referrer_user_id"]] = int(r["n"])
                for r in c.execute(
                    f"SELECT referrer_user_id, COUNT(*) AS n FROM referrals "
                    f"WHERE referrer_user_id IN ({placeholders}) AND converted_to_paid = 1 "
                    f"GROUP BY referrer_user_id",
                    chunk,
                ).fetchall():
                    converted_baseline[r["referrer_user_id"]] = int(r["n"])

    def _tier_for(uid: int) -> str:
        if uid in admin_ids:
            return "pro"
        plans = referrer_plans.get(uid, [])
        if any((p or "").startswith("pro") for p in plans):
            return "pro"
        if plans:
            return "trader"
        return "none"

    # Mutable per-referrer counter — preserves the "conversion N+1"
    # semantics of the original per-row SELECT as we stamp rows.
    already_count: dict[int, int] = dict(already_baseline)
    # ──────────────────────────────────────────────────────────────────────

    granted = 0
    skipped_no_payer = 0
    no_reward_at_this_count = 0
    emails_enqueued = 0

    for row in pending:
        referrer_id = row["referrer_user_id"]
        tier = _tier_for(referrer_id)
        if tier == "none":
            # Referrer isn't paying right now. Leave the row pending; if
            # they reactivate, the next daily run will grant the reward
            # retroactively. We don't want to mark it as forever-forfeited
            # for a lapsed card.
            skipped_no_payer += 1
            continue

        # How many of their referrals already have a reward stamped.
        # This is the conversion-number MINUS ONE that the reward logic
        # needs.
        already_n = already_count.get(referrer_id, 0)

        reward = referral_logic.compute_reward_for_referral(
            total_converted_before_this_one=already_n,
            current_tier=tier,
        )

        gift_id: int | None = None
        if reward is None:
            # Conversion 2, 3, 4, 6, 7, 8, 9 → no milestone reward.
            # Stamp as granted-but-null so we don't rescan it daily.
            ok = dbr.mark_referral_reward_granted(
                row["id"],
                reward_type="none",
                reward_months=0,
                reward_tier=None,
                gifted_subscription_id=None,
            )
            if ok:
                no_reward_at_this_count += 1
                already_count[referrer_id] = already_n + 1
            continue

        # Grant the reward via gifted_subscriptions.
        starts_at = int(time.time())
        ends_at = starts_at + reward["months"] * 30 * 86400
        try:
            with db.conn() as c:
                cur = c.execute(
                    "INSERT INTO gifted_subscriptions "
                    "(user_id, gifted_by_admin_id, subscription_type, "
                    " is_enterprise, starts_at, ends_at, is_permanent, "
                    " internal_notes, created_at) "
                    "VALUES (?, NULL, ?, 0, ?, ?, 0, ?, ?)",
                    (
                        referrer_id,
                        reward["tier"],
                        starts_at,
                        ends_at,
                        f"referral reward: {reward['type']} "
                        f"(conversion #{reward['conversion_number']})",
                        starts_at,
                    ),
                )
                gift_id = cur.lastrowid
        except Exception:
            log.exception(
                "failed to insert gifted_subscription for referral %s",
                row["id"],
            )
            continue  # leave the row pending; next run retries

        # Stamp the referral row + bump display counter.
        ok = dbr.mark_referral_reward_granted(
            row["id"],
            reward_type=reward["type"],
            reward_months=reward["months"],
            reward_tier=reward["tier"],
            gifted_subscription_id=gift_id,
        )
        if not ok:
            # Another run won the race — we've created an orphan gift.
            # Revoke it so the user isn't accidentally double-rewarded.
            log.warning(
                "referral %s already granted; revoking orphan gift %s",
                row["id"], gift_id,
            )
            try:
                with db.conn() as c:
                    c.execute(
                        "UPDATE gifted_subscriptions SET revoked = 1, "
                        "revoked_at = ?, "
                        "internal_notes = COALESCE(internal_notes, '') "
                        "  || ' [orphaned by race; auto-revoked]' "
                        "WHERE id = ?",
                        (int(time.time()), gift_id),
                    )
            except Exception:
                log.exception("orphan gift revoke failed")
            continue

        dbr.add_referral_credit_months(referrer_id, reward["months"])
        granted += 1
        already_count[referrer_id] = already_n + 1

        # Fire congratulations email via the job queue.
        try:
            from jobs.email_jobs import enqueue_email
            from backend.referrals import (
                format_reward_label,
                progress_toward_next_reward,
            )
            # converted_baseline + user_rows come from the batched
            # prefetch above. Every pending row is already paid, so the
            # converted-baseline doesn't move during this run. Fall back
            # to the per-user helpers if a referrer wasn't captured (a
            # defence-in-depth path that shouldn't fire in practice).
            total_converted = converted_baseline.get(referrer_id)
            if total_converted is None:
                total_converted = dbr.count_converted_referrals(referrer_id)
            progress = progress_toward_next_reward(total_converted)
            user_row = user_rows.get(referrer_id) or db.get_user_by_id(referrer_id)
            if user_row and user_row["email"]:
                await enqueue_email(
                    to=user_row["email"],
                    template="referral_reward",
                    context={
                        "display_name": (
                            user_row["username"]
                            or user_row["email"].split("@")[0]
                        ),
                        "referred_email": row["referred_email"] or "your referral",
                        "reward_label": format_reward_label(
                            reward["type"],
                            reward["months"],
                            reward["tier"],
                        ),
                        "total_converted": total_converted,
                        "next_milestone": progress["next_milestone"],
                        "next_reward_label": progress["next_reward_label"],
                        "referrals_url": f"{_app_url()}/settings/referrals",
                    },
                    tags=["referral"],
                )
                emails_enqueued += 1
        except Exception:
            # Email is best-effort; the gift is the real entitlement.
            log.exception("referral reward email failed for referral %s", row["id"])

    return {
        "processed": len(pending),
        "granted": granted,
        "no_reward_at_this_count": no_reward_at_this_count,
        "skipped_no_payer": skipped_no_payer,
        "emails_enqueued": emails_enqueued,
    }


# Daily at 02:15 UTC — 15 minutes after data_exports / other 02:00 jobs
# to avoid a thundering herd on the worker.
register_cron("process_referral_rewards", hour=2, minute=15)


# ── Leaderboard scorer ──────────────────────────────────────────────────────


@register_job("compute_user_leaderboard_scores")
async def compute_user_leaderboard_scores() -> dict[str, Any]:
    """Recompute user_accuracy rows for every opted-in user.

    Source of truth: the `user_predictions` table (migration 031). Each row
    is a subscriber-authored prediction that was later resolved with a
    correct/incorrect flag. We aggregate resolved rows per user, compute
    all-time and 90d / 30d / 7d windowed accuracies from resolved_at, and
    upsert the result into `user_accuracy` so the leaderboard API can
    ORDER BY ... accuracy_* directly.

    Users with zero resolved predictions get a row with NULL accuracy +
    total=0 so the leaderboard API can distinguish "opted in, no data yet"
    from "not opted in" without a second query.

    This is a full recompute rather than an incremental delta: the opt-in
    cohort is small (hundreds at most), user_predictions is bounded, and
    recomputing nightly means a change to the metric formula rolls out by
    the next cron tick with no migration needed.
    """
    import db
    import db_referrals as dbr

    now = int(time.time())
    cutoff_90d = now - 90 * 86400
    cutoff_30d = now - 30 * 86400
    cutoff_7d = now - 7 * 86400

    # Pull every opted-in user.
    with db.conn() as c:
        opted_in = c.execute(
            "SELECT id FROM users "
            "WHERE leaderboard_participation = 1 "
            "AND COALESCE(is_deleted, 0) = 0 "
            "AND COALESCE(suspended, 0) = 0"
        ).fetchall()

    # ── N+1 fix ──────────────────────────────────────────────────────────
    # The original loop ran one SELECT per opted-in user against
    # user_predictions. Batch-load every resolved prediction for the
    # cohort in chunks of 500 ids (under SQLITE_MAX_VARIABLE_NUMBER=999),
    # then partition by user_id in Python.
    opted_in_ids = [u["id"] for u in opted_in]
    rows_by_uid: dict[int, list[sqlite3.Row]] = {uid: [] for uid in opted_in_ids}
    if opted_in_ids:
        with db.conn() as c:
            for i in range(0, len(opted_in_ids), 500):
                chunk = opted_in_ids[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = c.execute(
                    f"SELECT user_id, resolved_at, resolved_correct "
                    f"FROM user_predictions "
                    f"WHERE user_id IN ({placeholders}) AND resolved = 1",
                    chunk,
                ).fetchall()
                for r in rows:
                    rows_by_uid[r["user_id"]].append(r)
    # ──────────────────────────────────────────────────────────────────────

    scored = 0
    unranked = 0
    for u in opted_in:
        all_rows = rows_by_uid.get(u["id"], [])

        total = len(all_rows)
        if total == 0:
            dbr.upsert_user_accuracy(
                u["id"],
                total=0, correct=0,
                accuracy_all=None,
                accuracy_90d=None,
                accuracy_30d=None,
                accuracy_7d=None,
            )
            unranked += 1
            continue

        def _acc(rows) -> float | None:
            n = len(rows)
            if n == 0:
                return None
            hits = sum(1 for r in rows if r["resolved_correct"])
            return hits / n

        all_correct = sum(1 for r in all_rows if r["resolved_correct"])
        rows_90d = [r for r in all_rows if (r["resolved_at"] or 0) >= cutoff_90d]
        rows_30d = [r for r in all_rows if (r["resolved_at"] or 0) >= cutoff_30d]
        rows_7d  = [r for r in all_rows if (r["resolved_at"] or 0) >= cutoff_7d]

        dbr.upsert_user_accuracy(
            u["id"],
            total=total,
            correct=all_correct,
            accuracy_all=_acc(all_rows),
            accuracy_90d=_acc(rows_90d),
            accuracy_30d=_acc(rows_30d),
            accuracy_7d=_acc(rows_7d),
        )
        scored += 1

    return {
        "opted_in": len(opted_in),
        "scored": scored,
        "unranked": unranked,
    }


register_cron("compute_user_leaderboard_scores", hour=3, minute=0)
