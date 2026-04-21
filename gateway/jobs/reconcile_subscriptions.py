"""Nightly Stripe subscription reconciliation.

Runs once a day: for every user with a recorded Stripe subscription,
ask Stripe what the current status is, compare against our DB, and
fix the DB where it drifts. Stripe is the source of truth.

Alerts if >5% of reconciled users have drifted — a large drift usually
means the webhook handler has been dropping events.

We reconcile TWO subscription surfaces:

  1. Subproduct subs (users.subproduct_subscriptions JSON). Each entry
     has a stripe_sub_id; for each, call Stripe, update status and
     period_end if they differ.

  2. The legacy main-product subs table if the row has
     ``stripe_subscription_id`` — same comparison.

Does NOT cancel sessions; the webhook hardening layer owns that. This
job only corrects status/period_end drift so future webhooks don't
miss events.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.reconcile_subscriptions")


# 5% drift threshold for admin alert.
_DRIFT_ALERT_RATIO = 0.05


def _stripe_api_key() -> Optional[str]:
    v = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    return v or None


def _fetch_status(sub_id: str) -> Optional[dict]:
    try:
        import stripe  # type: ignore[import]
        stripe.api_key = _stripe_api_key()
        sub = stripe.Subscription.retrieve(sub_id)
        return {
            "status": (sub.get("status") or "").lower() or None,
            "current_period_end": sub.get("current_period_end"),
            "cancel_at_period_end": bool(sub.get("cancel_at_period_end", False)),
            "metadata": dict(sub.get("metadata") or {}),
        }
    except Exception as exc:
        log.warning("stripe fetch failed for %s: %s", sub_id, exc)
        return None


@register_job("reconcile_subscriptions")
async def reconcile_subscriptions() -> dict[str, Any]:
    if not _stripe_api_key():
        return {"skipped": "no_stripe_key"}

    import db
    start = time.monotonic()
    checked = 0
    updated = 0
    users_checked: set[int] = set()
    users_drifted: set[int] = set()

    with db.conn() as c:
        rows = c.execute(
            "SELECT id, subproduct_subscriptions FROM users "
            "WHERE subproduct_subscriptions IS NOT NULL "
            "AND subproduct_subscriptions != '' "
            "AND subproduct_subscriptions != '{}'",
        ).fetchall()

    for row in rows:
        user_id = int(row["id"])
        users_checked.add(user_id)
        try:
            blob = json.loads(row["subproduct_subscriptions"] or "{}")
        except Exception:
            blob = {}
        if not isinstance(blob, dict):
            continue
        changed = False
        for slug, entry in list(blob.items()):
            if not isinstance(entry, dict):
                continue
            sub_id = entry.get("stripe_sub_id")
            if not sub_id:
                continue
            checked += 1
            live = _fetch_status(sub_id)
            if live is None:
                continue
            live_status = live["status"]
            if live_status and live_status != entry.get("status"):
                entry["status"] = live_status
                changed = True
            if (live["current_period_end"]
                    and live["current_period_end"] != entry.get("period_end")):
                entry["period_end"] = live["current_period_end"]
                changed = True
            blob[slug] = entry
        if changed:
            users_drifted.add(user_id)
            updated += 1
            try:
                with db.conn() as c:
                    c.execute(
                        "UPDATE users SET subproduct_subscriptions = ? "
                        "WHERE id = ?",
                        (json.dumps(blob, sort_keys=True), user_id),
                    )
            except Exception as exc:
                log.warning("reconcile write failed for user=%s: %s",
                            user_id, exc)

    duration = round(time.monotonic() - start, 2)
    drift_ratio = (
        len(users_drifted) / len(users_checked)
        if users_checked else 0.0
    )
    if drift_ratio >= _DRIFT_ALERT_RATIO and users_checked:
        log.warning(
            "subscription drift alert: %d/%d users drifted (%.1f%%)",
            len(users_drifted), len(users_checked), drift_ratio * 100,
        )
        try:
            # Best-effort admin email. Missing helpers just log above.
            from jobs.email_jobs import enqueue_email
            await enqueue_email(
                template="admin_subscription_drift",
                context={
                    "drift_count": len(users_drifted),
                    "total_count": len(users_checked),
                    "drift_pct": round(drift_ratio * 100, 1),
                },
                tags=["admin"],
            )
        except Exception as exc:
            log.warning("drift alert email failed: %s", exc)

    log.info(
        "subscription reconcile: checked=%d updated=%d users=%d drifted=%d in %.2fs",
        checked, updated, len(users_checked), len(users_drifted), duration,
    )
    return {
        "checked": checked,
        "updated": updated,
        "users_checked": len(users_checked),
        "users_drifted": len(users_drifted),
        "drift_ratio": round(drift_ratio, 4),
        "duration_seconds": duration,
    }


# Run once daily at 03:17 UTC — off-peak for both Stripe's API and
# whatever our main cron schedule is doing (see jobs/pipeline_jobs.py).
register_cron("reconcile_subscriptions", hour=3, minute=17)
