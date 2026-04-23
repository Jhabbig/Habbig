"""Monthly "what shipped from your feedback" digest — ENHANCEMENT #7.

Pulls every ``feedback_items`` row that went ``status='shipped'`` in the
last 30 days, then for each subscribed user queues one ``send_email``
job whose context lists the shipped items they voted on or submitted.
Payload-only: the SMTP transport itself wires up separately, so this
job just writes rows into the queue audit log and trusts the worker to
deliver them.

Schedule: 06:00 UTC on the 1st of every month. We deliberately don't
fan out on a rolling weekly cadence — once a month is the cadence the
product spec calls for, and it keeps the risk of spamming churned
users low if the cron misfires twice.

Test hook: ``compute_feedback_digest_sync()`` runs the full enqueue
cycle without needing the worker loop, and ``DIGEST_DRY_RUN=1`` (env)
skips the ``enqueue_job`` call so tests can inspect the recipient list
without mocking the backend. The dry-run recipients come back in the
return payload.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.feedback_digest")


# Max recipients per run so a misconfigured rollout can't fan out a
# million emails in one go. 10_000 leaves plenty of headroom for the
# current user base and is easy to raise if the product grows.
MAX_RECIPIENTS = 10_000

# Window for "recently shipped" items. 30 days matches the monthly
# cadence — anything older was already in a prior digest.
DIGEST_WINDOW_DAYS = 30


def _shipped_items(c, window_days: int) -> list[dict]:
    """Return shipped items closed in the last ``window_days`` days."""
    rows = c.execute(
        """
        SELECT id, title, shipped_commit_sha, updated_at
        FROM feedback_items
        WHERE status = 'shipped'
          AND updated_at >= datetime('now', ?)
        ORDER BY updated_at DESC
        """,
        (f"-{int(window_days)} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def _recipients_for(c, item_ids: list[int]) -> dict[int, list[int]]:
    """Return {user_id: [item_id, ...]} for every subscriber who either
    submitted or voted on any of the shipped items.

    Filters to active paid subscribers (``__plan__`` row with plan set +
    status 'active'). Free accounts don't get the digest — they opted
    out of the funnel implicitly. Admins included iff they have a plan.
    """
    if not item_ids:
        return {}

    placeholders = ",".join("?" * len(item_ids))
    rows = c.execute(
        f"""
        SELECT DISTINCT u.id AS user_id, v.feedback_id AS item_id
        FROM users u
        JOIN feedback_votes v ON v.user_id = u.id
        JOIN subscriptions s ON s.user_id = u.id
          AND s.dashboard_key = '__plan__'
          AND s.status = 'active'
          AND s.plan IS NOT NULL
          AND s.plan != ''
        WHERE v.feedback_id IN ({placeholders})
        UNION
        SELECT DISTINCT u.id AS user_id, fi.id AS item_id
        FROM users u
        JOIN feedback_items fi ON fi.user_id = u.id
        JOIN subscriptions s ON s.user_id = u.id
          AND s.dashboard_key = '__plan__'
          AND s.status = 'active'
          AND s.plan IS NOT NULL
          AND s.plan != ''
        WHERE fi.id IN ({placeholders})
        """,
        (*item_ids, *item_ids),
    ).fetchall()

    out: dict[int, list[int]] = {}
    for r in rows:
        uid = int(r["user_id"])
        iid = int(r["item_id"])
        out.setdefault(uid, []).append(iid)
    return out


def _email_for(c, user_id: int) -> str | None:
    r = c.execute(
        "SELECT email FROM users WHERE id = ? AND suspended = 0",
        (user_id,),
    ).fetchone()
    return r["email"] if r else None


def compute_feedback_digest_sync(
    *,
    window_days: int = DIGEST_WINDOW_DAYS,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Synchronous core — importable by tests without the async backend.

    Returns {"shipped": N, "queued": M, "recipients": [...]}. The
    ``recipients`` list is only populated in dry_run mode; in a normal
    run it stays empty to keep the return payload tiny.
    """
    import db

    if dry_run is None:
        dry_run = os.environ.get("DIGEST_DRY_RUN") == "1"

    with db.conn() as c:
        items = _shipped_items(c, window_days=window_days)
        if not items:
            log.info("feedback digest: nothing shipped in %dd", window_days)
            return {"shipped": 0, "queued": 0, "recipients": []}

        item_ids = [i["id"] for i in items]
        by_user = _recipients_for(c, item_ids)

        recipients: list[dict[str, Any]] = []
        queued = 0
        for user_id, ids_for_user in list(by_user.items())[:MAX_RECIPIENTS]:
            email = _email_for(c, user_id)
            if not email:
                continue
            # Intersect the user's matched items with the full shipped
            # set so the email context carries full titles, not just ids.
            user_items = [
                {"id": i["id"], "title": i["title"], "sha": i.get("shipped_commit_sha")}
                for i in items if i["id"] in ids_for_user
            ]
            payload = {
                "to": email,
                "template": "feedback_shipped_digest",
                "context": {
                    "user_id": user_id,
                    "shipped_items": user_items,
                    "window_days": window_days,
                },
                "tags": ["feedback", "digest"],
            }
            recipients.append({"user_id": user_id, "email": email, "items": user_items})
            if not dry_run:
                _enqueue_send_email(payload)
            queued += 1

    log.info(
        "feedback digest: shipped=%d queued=%d dry_run=%s",
        len(items), queued, dry_run,
    )
    return {
        "shipped": len(items),
        "queued": queued,
        "recipients": recipients if dry_run else [],
    }


def _enqueue_send_email(payload: dict) -> None:
    """Best-effort enqueue. If the backend isn't running (test without
    worker) or the event loop isn't available, fall back to a direct
    run_until call — tests can mock jobs.enqueue_job to short-circuit."""
    try:
        import asyncio
        from jobs import enqueue_job
    except Exception as exc:
        log.warning("enqueue_job import failed: %s", exc)
        return
    coro = enqueue_job(
        "send_email",
        to=payload["to"],
        template=payload["template"],
        context=payload["context"],
        tags=payload["tags"],
    )
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(coro)
            return
    except RuntimeError:
        loop = None
    try:
        asyncio.run(coro)
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("feedback digest enqueue failed: %s", exc)


@register_job("feedback_shipped_digest")
async def feedback_shipped_digest(ctx=None) -> dict[str, Any]:
    return compute_feedback_digest_sync()


# 06:00 UTC on the 1st of every month. Staggered off the hour-top so
# we don't collide with other midnight-aligned jobs.
register_cron("feedback_shipped_digest", day=1, hour=6, minute=3)
