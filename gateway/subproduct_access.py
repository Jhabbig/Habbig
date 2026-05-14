"""Per-user subproduct entitlement checks.

Three-layer access model:

  1. Super admins + pro/enterprise tier → access to everything. They
     bypass the subproduct blob and never hit Stripe.
  2. Users with a subproduct-specific subscription (from
     ``users.subproduct_subscriptions`` JSON) → access if status=='active'
     and period_end in the future.
  3. Everyone else → denied with HTTP 402 (Payment Required). The 402
     is deliberate: 403 means "authenticated but no permission ever";
     402 means "pay and retry".

``require_subproduct_access(slug)`` is a FastAPI dependency factory
that route handlers attach to sub-brand endpoints. The default behaviour
for non-pro users in production is to *verify with Stripe live* the
first time they hit a protected endpoint, then cache the verdict for
5 minutes. This catches subscriptions Stripe has deleted but our webhook
hasn't seen yet (subscription lapse → eventual lockout instead of waiting
indefinitely for the next webhook).

The cache is in-process (dict keyed by user_id + slug). For multi-worker
deployments that would drift, but we only run one uvicorn worker today.
Redis would be a future swap; the existing ``cache.CacheService`` already
supports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from threading import Lock
from typing import Any, Optional

from fastapi import HTTPException, Request


log = logging.getLogger("subproduct.access")


# Super-admin role levels that skip the paywall. Matches the existing
# convention: users.is_admin == 2 is super-admin, 1 is regular admin,
# 0 is user.
_SUPER_ADMIN_LEVEL = 2
_ADMIN_LEVEL = 1

# Short-lived positive cache for live Stripe verification. Map (user_id,
# slug) → (expires_at, verdict). No negative cache — if Stripe says the
# sub was cancelled we want the very next request to see it.
_verify_cache: dict[tuple[int, str], tuple[float, bool]] = {}
_verify_lock = Lock()
_VERIFY_TTL_SECONDS = 300


def _pro_or_better(user_row: Any) -> bool:
    """Return True for admins + pro/enterprise tiers.

    Accepts either a dict or an sqlite3.Row. The tier check is generous:
    any tier name containing 'pro' or 'enterprise' qualifies, which
    covers 'pro_annual', 'enterprise_team', etc.
    """
    if user_row is None:
        return False
    level = _field(user_row, "is_admin", 0) or 0
    if int(level) >= _ADMIN_LEVEL:
        return True
    tier = (_field(user_row, "subscription_tier", "") or "").lower()
    return "pro" in tier or "enterprise" in tier


def _field(row: Any, name: str, default=None):
    """Uniform accessor for dict / sqlite3.Row / object."""
    try:
        if isinstance(row, dict):
            return row.get(name, default)
        # sqlite3.Row supports indexing but not .get()
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def _blob_entry(user_row: Any, slug: str) -> Optional[dict]:
    """Parse users.subproduct_subscriptions JSON and pull the slug entry."""
    raw = _field(user_row, "subproduct_subscriptions", "") or ""
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except (TypeError, ValueError):
        return None
    entry = blob.get(slug) if isinstance(blob, dict) else None
    if not isinstance(entry, dict):
        return None
    return entry


def has_subproduct_access(user_row: Any, subproduct_slug: str) -> bool:
    """Deterministic access check — no network, DB or Stripe calls.

    Returns True iff the user row indicates a valid entitlement:
    super-admin, pro/enterprise tier, or an ``active`` subproduct
    subscription whose period_end has not elapsed.
    """
    if user_row is None:
        return False
    if _pro_or_better(user_row):
        return True
    entry = _blob_entry(user_row, subproduct_slug)
    if not entry:
        return False
    status = (entry.get("status") or "").lower()
    if status != "active":
        return False
    # period_end is an integer epoch second; a missing value is treated
    # as "unknown", which we reject — the webhook should always write
    # it when the subscription is created.
    period_end = entry.get("period_end")
    try:
        if period_end is None or int(period_end) <= int(time.time()):
            return False
    except (TypeError, ValueError):
        return False
    return True


def invalidate_user(user_id: int, slug: Optional[str] = None) -> None:
    """Drop cached verify verdicts for ``user_id``.

    Called from the Stripe webhook after status changes so the very
    next request re-verifies rather than serving the stale cached
    verdict.
    """
    with _verify_lock:
        if slug:
            _verify_cache.pop((user_id, slug), None)
        else:
            for key in list(_verify_cache):
                if key[0] == user_id:
                    _verify_cache.pop(key, None)


def _cached_verify(user_id: int, slug: str) -> Optional[bool]:
    now = time.time()
    with _verify_lock:
        entry = _verify_cache.get((user_id, slug))
        if not entry:
            return None
        expires_at, verdict = entry
        if expires_at < now:
            _verify_cache.pop((user_id, slug), None)
            return None
        return verdict


def _store_verify(user_id: int, slug: str, verdict: bool) -> None:
    expires_at = time.time() + _VERIFY_TTL_SECONDS
    with _verify_lock:
        _verify_cache[(user_id, slug)] = (expires_at, verdict)


async def _live_stripe_status(entry: dict) -> Optional[str]:
    """Fetch the live subscription status from Stripe.

    Returns the status string (e.g. ``'active'``, ``'past_due'``,
    ``'canceled'``) or None if Stripe is unreachable / not configured.
    Callers treat None as "fall back to local DB state".

    The Stripe SDK call is synchronous and blocks ~150-500ms; we run it
    on a worker thread so the event loop stays free for other requests.
    """
    sub_id = entry.get("stripe_sub_id")
    api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not sub_id or not api_key:
        return None
    try:  # Lazy import so the dep is optional in dev/test.
        import stripe  # type: ignore[import]
        stripe.api_key = api_key
        sub = await asyncio.to_thread(stripe.Subscription.retrieve, sub_id)
        return (sub.get("status") or "").lower() or None
    except Exception as exc:
        log.warning("live stripe verify failed for %s: %s", sub_id, exc)
        return None


def require_subproduct_access(slug: str):
    """Build a FastAPI dependency that enforces access to ``slug``.

    The dependency inspects ``request.state`` for the attached subproduct
    (set by SubproductMiddleware) and ``request.state.user`` for the
    resolved narve session. A mismatched subproduct — e.g. someone
    hitting ``/api/sports/best`` from ``crypto.narve.ai`` — is a 402
    as if they didn't own the subproduct at all, because the routing
    rule is the whole point.

    Route usage:

        @router.get("/api/sports/best", dependencies=[
            Depends(require_subproduct_access("sports"))
        ])
        async def sports_best(...): ...
    """
    async def _dep(request: Request) -> None:
        attached = getattr(request.state, "subproduct", None)
        user = getattr(request.state, "user", None)

        # If the caller is on a sub-brand host, it must match the
        # protected slug — otherwise a user with sports access could
        # hit sports-only endpoints via any subdomain.
        if attached is not None and attached != slug:
            raise HTTPException(
                status_code=402,
                detail=f"This endpoint is {slug}-only; you are on {attached}.narve.ai",
            )

        if user is None:
            raise HTTPException(status_code=402, detail="Subscription required")

        # Fast path: pro/admin row → allow, no Stripe hit.
        if _pro_or_better(user):
            return

        # Local DB says no → nothing to verify, reject early.
        if not has_subproduct_access(user, slug):
            raise HTTPException(
                status_code=402,
                detail=f"{slug} subscription required",
            )

        # Local DB says yes, but for non-pro users in production we
        # verify with Stripe once per 5 minutes to catch a lapse that
        # hasn't hit our webhook yet.
        if os.environ.get("PRODUCTION", "0") != "1":
            return

        user_id = int(_field(user, "id", 0) or _field(user, "user_id", 0) or 0)
        if not user_id:
            return  # No id to cache against — fall through.

        cached = _cached_verify(user_id, slug)
        if cached is True:
            return
        if cached is False:
            raise HTTPException(
                status_code=402,
                detail=f"{slug} subscription inactive",
            )

        entry = _blob_entry(user, slug) or {}
        live = await _live_stripe_status(entry)
        if live is None:
            # Stripe unreachable — trust the DB (we already passed
            # has_subproduct_access above). Don't cache either verdict.
            return
        ok = live == "active"
        _store_verify(user_id, slug, ok)
        if not ok:
            raise HTTPException(
                status_code=402,
                detail=f"{slug} subscription {live}",
            )

    return _dep
