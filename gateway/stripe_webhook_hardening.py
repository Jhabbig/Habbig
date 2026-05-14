"""Reusable hardening helpers for the Stripe webhook handler.

Designed so the existing handler in server.py calls these BEFORE and
AFTER its per-event branches. Keeps all the idempotency + livemode +
extended cancel logic in one place that's easy to unit test without
standing up the full FastAPI app.

Typical call pattern inside the existing handler:

    from stripe_webhook_hardening import (
        reject_mode_mismatch, mark_received, mark_processed,
        apply_subscription_cancelled, apply_invoice_payment_failed,
    )

    # After signature verification (keep the existing check):
    mismatch = reject_mode_mismatch(event)
    if mismatch: return mismatch  # JSONResponse(400)

    already = mark_received(event)
    if already: return already    # JSONResponse({"status": "already_processed"})

    try:
        # ... existing per-event branches ...
        if event["type"] == "customer.subscription.deleted":
            apply_subscription_cancelled(event)
        elif event["type"] == "invoice.payment_failed":
            apply_invoice_payment_failed(event)
        # ... etc
        mark_processed(event)
    except Exception as exc:
        mark_processed(event, error=str(exc))
        raise

None of these helpers 500. If the DB is unavailable they log + allow
the event through so a transient issue doesn't amplify into missed
webhooks (Stripe retries, so the handler will see it again).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
from typing import Any, Optional

from fastapi.responses import JSONResponse


log = logging.getLogger("stripe.hardening")


def _is_production() -> bool:
    return os.environ.get("PRODUCTION", "0") == "1"


# ── Stripe IP allowlist (defence-in-depth) ─────────────────────────────────
#
# Source: https://stripe.com/files/ips/ips_webhooks.txt — pull annually
# If Stripe rotates IPs and we get false rejections, refresh from the
# canonical URL. Webhook signature still defends us; allowlist is
# defence-in-depth.
#
# Snapshot: 2026-05-14. Stripe's published list rarely changes but is
# authoritative — re-fetch yearly or whenever the audit log flags
# unexpected 403s on /stripe/webhook.
_STRIPE_WEBHOOK_CIDRS = (
    "3.18.12.63/32",
    "3.130.192.231/32",
    "13.235.14.237/32",
    "13.235.122.149/32",
    "18.211.135.69/32",
    "35.154.171.200/32",
    "52.15.183.38/32",
    "54.88.130.119/32",
    "54.88.130.237/32",
    "54.187.174.169/32",
    "54.187.205.235/32",
    "54.187.216.72/32",
)

_STRIPE_NETWORKS = tuple(
    ipaddress.ip_network(cidr) for cidr in _STRIPE_WEBHOOK_CIDRS
)


def _is_stripe_ip(addr: str) -> bool:
    """Return True if ``addr`` is a literal Stripe webhook source IP."""
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _STRIPE_NETWORKS)


def _allowlist_enforced() -> bool:
    """Whether the allowlist check should reject non-Stripe IPs.

    Default behavior:
      * ``STRIPE_IP_ALLOWLIST_ENFORCE`` unset → enforce in production,
        bypass in dev/test so local replay tooling still works.
      * Explicit ``true`` / ``false`` overrides the default in either
        direction (e.g. enforce on staging that doesn't set PRODUCTION).
    """
    flag = os.environ.get("STRIPE_IP_ALLOWLIST_ENFORCE")
    if flag is None:
        return _is_production()
    return str(flag).strip().lower() in {"1", "true", "yes", "on"}


def reject_non_stripe_ip(client_ip: str) -> Optional[JSONResponse]:
    """Reject the request if ``client_ip`` is not in the Stripe allowlist.

    Call this in the webhook route AFTER extracting the real client IP
    (via ``CF-Connecting-IP`` header) and BEFORE signature verification.
    Returns a ``JSONResponse`` if the caller should abort with 403, else
    ``None``.

    Defence-in-depth — the Stripe signature verification still defends
    us against forged payloads; this guards against any attacker who
    discovered the signing secret but can't spoof Stripe's source IPs.
    """
    if not _allowlist_enforced():
        return None
    if _is_stripe_ip(client_ip):
        return None
    log.warning(
        "Stripe webhook from non-Stripe IP %s — rejecting", client_ip,
    )
    return JSONResponse({"error": "Forbidden"}, status_code=403)


def extract_client_ip(request) -> str:
    """Pull the real client IP from a FastAPI ``Request``.

    Prefers ``CF-Connecting-IP`` (set by Cloudflare on every request to
    the origin) over ``request.client.host`` (which under Cloudflare
    would be a Cloudflare edge IP, not the real source).
    """
    cf_ip = request.headers.get("CF-Connecting-IP") or ""
    if cf_ip:
        return cf_ip.strip()
    try:
        return request.client.host or ""
    except AttributeError:
        return ""


def reject_mode_mismatch(event: dict) -> Optional[JSONResponse]:
    """Reject a test-mode event in production (and vice versa).

    Stripe sets ``livemode`` on every event. A livemode=False event
    hitting production means either (a) a stray test webhook URL or
    (b) an attacker forwarding events from a test account. Either
    way, reject.

    Returns a ``JSONResponse`` if the caller should abort, else None.
    """
    livemode = bool(event.get("livemode", False))
    prod = _is_production()
    if livemode != prod:
        log.warning(
            "stripe mode mismatch: livemode=%s production=%s type=%s id=%s",
            livemode, prod, event.get("type"), event.get("id"),
        )
        return JSONResponse(
            {"error": "Event mode does not match environment"},
            status_code=400,
        )
    return None


def mark_received(event: dict) -> Optional[JSONResponse]:
    """Insert the event_id into processed_stripe_events.

    Returns:
      * ``JSONResponse({"status": "already_processed"})`` if the event
        has been seen before (handler should short-circuit).
      * ``None`` if this is the first time — caller proceeds.

    The INSERT is ``INSERT OR IGNORE``; if the UNIQUE constraint fires
    the row already exists, meaning a prior handler invocation already
    processed (or started processing) this event.
    """
    event_id = event.get("id")
    event_type = event.get("type") or ""
    livemode = 1 if event.get("livemode") else 0
    now = int(time.time())
    if not event_id:
        return None  # unexpected shape; let the handler deal with it

    try:
        import db
        with db.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO processed_stripe_events "
                "(event_id, event_type, livemode, received_at) "
                "VALUES (?, ?, ?, ?)",
                (event_id, event_type, livemode, now),
            )
            if cur.rowcount == 0:
                # Already seen. Still return 200 — Stripe retries on
                # non-2xx and we don't want retries when we've already
                # processed.
                return JSONResponse({"status": "already_processed"})
    except Exception as exc:
        log.warning("stripe idempotency record failed: %s", exc)
    return None


def mark_processed(event: dict, error: Optional[str] = None) -> None:
    """Stamp processed_at on the event row.

    Call at the END of the handler — on both success and failure. The
    ``error`` field is populated when the handler caught an exception;
    the admin panel surfaces rows with non-null ``error``.
    """
    event_id = event.get("id")
    if not event_id:
        return
    now = int(time.time())
    try:
        import db
        with db.conn() as c:
            c.execute(
                "UPDATE processed_stripe_events SET "
                "  processed_at = ?, error = ? "
                "WHERE event_id = ?",
                (now, (error or None), event_id),
            )
    except Exception as exc:
        log.warning("stripe processed_at update failed: %s", exc)


# ── Extended cancel / payment-failed branches ──────────────────────────────


def _user_id_from_event(event: dict) -> Optional[int]:
    """Pull user_id out of the common metadata paths Stripe uses."""
    obj = (event.get("data") or {}).get("object") or {}
    # Subscription object (customer.subscription.*): metadata sits on
    # the subscription itself.
    meta = obj.get("metadata") or {}
    uid = meta.get("user_id") or meta.get("narve_user_id")
    if uid:
        try:
            return int(uid)
        except (TypeError, ValueError):
            pass
    # Invoice events (invoice.payment_failed): the subscription is a
    # nested field by ID. Fall back to mapping via the customer's
    # stripe_customer_id if we store it.
    customer = obj.get("customer")
    if customer:
        try:
            import db
            with db.conn() as c:
                row = c.execute(
                    "SELECT id FROM users WHERE stripe_customer_id = ?",
                    (customer,),
                ).fetchone()
            if row:
                return int(row["id"])
        except Exception:
            pass
    return None


def apply_subscription_cancelled(event: dict) -> None:
    """Handle customer.subscription.deleted.

    * Write subproduct_subscriptions[slug].status = "canceled" if the
      cancelled sub had a subproduct slug in its metadata.
    * Revoke all sessions for the user so they can't keep using stale
      cookies.
    * Invalidate the in-process access cache so the next request
      re-verifies.
    * Deactivate any embed widgets the user published.
    * Enqueue the ``subscription_cancelled`` email.
    """
    obj = (event.get("data") or {}).get("object") or {}
    user_id = _user_id_from_event(event)
    meta = obj.get("metadata") or {}
    sub_slug = (meta.get("subproduct_slug") or "").strip() or None

    try:
        import db
        if user_id and sub_slug:
            _update_subproduct_status(user_id, sub_slug, "canceled")
        if user_id:
            with db.conn() as c:
                # Best-effort revoke — names vary across schema revisions.
                for table in ("narve_sessions", "sessions", "user_sessions"):
                    try:
                        c.execute(
                            f"UPDATE {table} SET revoked = 1 "
                            f"WHERE user_id = ? AND COALESCE(revoked, 0) = 0",
                            (user_id,),
                        )
                    except Exception:
                        pass
                try:
                    c.execute(
                        "UPDATE embed_widgets SET is_active = 0 "
                        "WHERE user_id = ?",
                        (user_id,),
                    )
                except Exception:
                    pass
    except Exception as exc:
        log.warning("subscription cancelled DB updates failed: %s", exc)

    # Drop cached access verdicts so the next API call re-checks.
    if user_id:
        try:
            from subproduct_access import invalidate_user
            invalidate_user(user_id)
        except Exception as exc:
            log.warning("access cache invalidate failed: %s", exc)

    # Enqueue the cancellation email.
    try:
        import asyncio
        import db
        from jobs.email_jobs import enqueue_email
        if user_id:
            row = db.get_user_by_id(user_id)
            user_email = row["email"] if row and "email" in row.keys() else None
            if user_email:
                # Stripe gives us cancel_at / canceled_at on the subscription
                # object — surface it to the template so the body reads
                # "expired on <date>" rather than the empty fallback.
                import datetime as _dt
                ts = obj.get("cancel_at") or obj.get("canceled_at") or obj.get("ended_at")
                period_end_date = ""
                if ts:
                    try:
                        period_end_date = _dt.date.fromtimestamp(int(ts)).isoformat()
                    except Exception:
                        period_end_date = ""
                coro = enqueue_email(
                    to=user_email,
                    template="subscription_cancelled",
                    context={
                        "user_id": user_id,
                        "subproduct_slug": sub_slug or "",
                        "period_end_date": period_end_date,
                    },
                    tags=["subscription_cancelled"],
                )
                # When called from a sync handler, schedule the coroutine on
                # the running loop. In the job path we're already async.
                try:
                    asyncio.get_event_loop().create_task(coro)  # type: ignore[arg-type]
                except RuntimeError:
                    asyncio.run(coro)  # type: ignore[arg-type]
    except Exception as exc:
        log.warning("cancellation email enqueue failed: %s", exc)


def apply_invoice_payment_failed(event: dict) -> None:
    """Handle invoice.payment_failed — mark the subproduct past_due
    so the gate re-verifies and hide embed widgets pending payment."""
    obj = (event.get("data") or {}).get("object") or {}
    user_id = _user_id_from_event(event)
    # Metadata for invoices is on the subscription, which is an ID
    # here. We fetch the subscription via Stripe to read its metadata.
    sub_id = obj.get("subscription")
    sub_slug: Optional[str] = None
    if sub_id:
        sub_slug = _lookup_subproduct_slug(sub_id)

    try:
        if user_id and sub_slug:
            _update_subproduct_status(user_id, sub_slug, "past_due")
    except Exception as exc:
        log.warning("payment_failed DB update failed: %s", exc)

    if user_id:
        try:
            from subproduct_access import invalidate_user
            invalidate_user(user_id)
        except Exception:
            pass


# ── Private helpers ────────────────────────────────────────────────────────


def _update_subproduct_status(user_id: int, slug: str, status: str) -> None:
    """Mutate ``users.subproduct_subscriptions[slug].status``."""
    import db
    with db.conn() as c:
        row = c.execute(
            "SELECT subproduct_subscriptions FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return
        raw = row["subproduct_subscriptions"] or "{}"
        try:
            blob = json.loads(raw)
        except (TypeError, ValueError):
            blob = {}
        if not isinstance(blob, dict):
            blob = {}
        entry = blob.get(slug) or {}
        if not isinstance(entry, dict):
            entry = {}
        entry["status"] = status
        blob[slug] = entry
        c.execute(
            "UPDATE users SET subproduct_subscriptions = ? WHERE id = ?",
            (json.dumps(blob, sort_keys=True), user_id),
        )
    # Tier/add-on changed upstream — invalidate their cached feed + every
    # tier-scoped best-bets page. Deferred import to keep the webhook path
    # tolerant of a missing cache module in lightweight test harnesses.
    try:
        from cache import ttl_invalidate
        ttl_invalidate.on_subscription_change(user_id)
    except Exception:
        log.exception("ttl_invalidate.on_subscription_change failed (user=%s)", user_id)


def _lookup_subproduct_slug(sub_id: str) -> Optional[str]:
    """Fetch a Stripe subscription and return its metadata.subproduct_slug."""
    api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not api_key:
        return None
    try:
        import stripe  # type: ignore[import]
        stripe.api_key = api_key
        sub = stripe.Subscription.retrieve(sub_id)
        return (sub.get("metadata") or {}).get("subproduct_slug") or None
    except Exception as exc:
        log.warning("stripe subscription lookup failed: %s", exc)
        return None
