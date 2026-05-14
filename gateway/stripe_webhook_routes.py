"""Stripe webhook handler — POST /stripe/webhook.

Wires :mod:`stripe_webhook_hardening` helpers into a real FastAPI
route on ``server.app``. Loaded by side-effect of import, same pattern
as :mod:`billing_routes`.

Check ordering (every check MUST run before any state mutation):

  1. Library check — ``stripe`` Python SDK must be importable.
     Missing -> 503 "Stripe integration not configured".
  2. Global rate limit — 100 req/min. Stripe normally bursts ~3/s;
     anything beyond is an attack or a misconfigured replay loop.
  3. IP allowlist — :func:`extract_client_ip` then
     :func:`reject_non_stripe_ip`. Bypassed in dev unless
     ``STRIPE_IP_ALLOWLIST_ENFORCE=true``.
  4. Signature verification — ``stripe.Webhook.construct_event``
     with ``STRIPE_WEBHOOK_SECRET``. SignatureVerificationError -> 400
     with no side effects.
  5. Live-mode gate — ``STRIPE_LIVE_MODE=true`` required to accept
     ``livemode=True`` events. Default false so dev never touches
     real money even if env leaks.
  6. Idempotency — :func:`mark_received` short-circuits replays with
     200 status=already_processed.
  7. Dispatch — branch on ``event["type"]``; wrapped in try/except
     so a broken branch can't take down the route.
  8. Always 200 on accepted events. The four reject cases above use
     403/400/503/429. Stripe retries non-2xx for 3 days so we'd
     rather log and stamp an error than amplify retries.

``backend/payments/stripe_stub.py`` still raises NotImplementedError
so any code path that imports the stub instead of this module fails
loudly rather than silently accepting unsigned events.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

import db
from server import app, _is_rate_limited
from stripe_webhook_hardening import (
    apply_invoice_payment_failed,
    apply_subscription_cancelled,
    extract_client_ip,
    mark_processed,
    mark_received,
    reject_non_stripe_ip,
)


log = logging.getLogger("stripe.webhook")


def _stripe_live_mode_enabled() -> bool:
    """``STRIPE_LIVE_MODE=true`` (or 1/yes/on) opts in to accepting
    ``livemode=True`` Stripe events. Default false so a fresh dev box
    cannot accidentally honour production webhooks."""
    flag = os.environ.get("STRIPE_LIVE_MODE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _stripe_webhook_secret() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def _coerce_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── Dispatch branches ──────────────────────────────────────────────────────


def _grant_access(event: dict) -> None:
    """customer.subscription.created — write the subscriptions row.

    Metadata contract: ``user_id`` + ``dashboard_key`` (or alias
    ``subproduct_slug``); ``plan`` optional. UPSERT on the
    (user_id, dashboard_key) unique key so a misordered Stripe event
    sequence doesn't crash on duplicates."""
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    user_id = _coerce_int(meta.get("user_id") or meta.get("narve_user_id"))
    dashboard_key = (
        meta.get("dashboard_key") or meta.get("subproduct_slug") or ""
    ).strip()
    plan = (meta.get("plan") or "default").strip() or "default"
    stripe_sub_id = obj.get("id") or ""

    if not user_id or not dashboard_key:
        log.warning(
            "subscription.created missing metadata: user_id=%s key=%s id=%s",
            user_id, dashboard_key, event.get("id"),
        )
        return

    now = int(time.time())
    try:
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions "
                "(user_id, dashboard_key, plan, status, started_at, "
                " stripe_sub_id, source) "
                "VALUES (?, ?, ?, 'active', ?, ?, 'stripe') "
                "ON CONFLICT(user_id, dashboard_key) DO UPDATE SET "
                "  plan = excluded.plan, "
                "  status = 'active', "
                "  stripe_sub_id = excluded.stripe_sub_id, "
                "  source = 'stripe'",
                (user_id, dashboard_key, plan, now, stripe_sub_id),
            )
    except Exception as exc:
        log.warning("grant_access DB write failed: %s", exc)


def _update_plan(event: dict) -> None:
    """customer.subscription.updated — sync plan + status from Stripe.

    Collapses any non-active Stripe lifecycle state ("incomplete",
    "past_due", "unpaid", etc.) to ``inactive`` locally — the UI only
    needs to know "user has it" vs "user doesn't"."""
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    user_id = _coerce_int(meta.get("user_id") or meta.get("narve_user_id"))
    dashboard_key = (
        meta.get("dashboard_key") or meta.get("subproduct_slug") or ""
    ).strip()
    plan = (meta.get("plan") or "").strip()
    stripe_status = (obj.get("status") or "active").strip()
    local_status = "active" if stripe_status in {"active", "trialing"} else "inactive"

    if not user_id or not dashboard_key:
        log.warning(
            "subscription.updated missing metadata: user_id=%s key=%s id=%s",
            user_id, dashboard_key, event.get("id"),
        )
        return

    try:
        with db.conn() as c:
            params = [local_status]
            sets = ["status = ?"]
            if plan:
                sets.append("plan = ?")
                params.append(plan)
            params.extend([user_id, dashboard_key])
            c.execute(
                f"UPDATE subscriptions SET {', '.join(sets)} "
                f"WHERE user_id = ? AND dashboard_key = ?",
                params,
            )
    except Exception as exc:
        log.warning("update_plan DB write failed: %s", exc)


def _record_payment(event: dict) -> None:
    """invoice.paid — flip a past_due subscription back to active.
    Active subscriptions are left alone; the payment is the signal
    that the dunning window has closed."""
    obj = (event.get("data") or {}).get("object") or {}
    customer = obj.get("customer") or ""
    sub_id = obj.get("subscription") or ""
    if not sub_id:
        return
    try:
        with db.conn() as c:
            c.execute(
                "UPDATE subscriptions SET status = 'active' "
                "WHERE stripe_sub_id = ? AND status = 'past_due'",
                (sub_id,),
            )
    except Exception as exc:
        log.warning(
            "record_payment DB write failed: %s (sub=%s cust=%s)",
            exc, sub_id, customer,
        )


# ── Route ──────────────────────────────────────────────────────────────────


@app.post("/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request):
    """Webhook entrypoint. See module docstring for check ordering.

    Status codes:
      503 — stripe SDK not installed
      429 — rate-limit exceeded (100/min global)
      403 — non-Stripe source IP (when allowlist enforced)
      400 — bad signature / mode mismatch
      200 — accepted (processed, replayed, or ignored)
    """
    # 1) Stripe SDK presence.
    try:
        import stripe  # type: ignore[import]
    except ImportError:
        log.warning("stripe library not installed — returning 503")
        return JSONResponse(
            {"error": "Stripe integration not configured"},
            status_code=503,
        )

    # 2) Global rate limit.
    if _is_rate_limited("stripe_webhook_global", limit=100, window=60):
        log.warning("stripe webhook global rate limit triggered")
        return JSONResponse(
            {"error": "Rate limit exceeded"}, status_code=429,
        )

    # 3) IP allowlist.
    client_ip = extract_client_ip(request)
    blocked = reject_non_stripe_ip(client_ip)
    if blocked is not None:
        return blocked

    # 4) Read body + signature header.
    try:
        payload = await request.body()
    except Exception as exc:
        log.warning("failed to read webhook body: %s", exc)
        return JSONResponse({"error": "Bad request"}, status_code=400)

    sig_header = (
        request.headers.get("Stripe-Signature")
        or request.headers.get("stripe-signature")
        or ""
    )
    secret = _stripe_webhook_secret()

    # 5) Signature verification.
    if secret:
        try:
            event = stripe.Webhook.construct_event(  # type: ignore[attr-defined]
                payload, sig_header, secret,
            )
            if hasattr(event, "to_dict"):
                event = event.to_dict()
            elif not isinstance(event, dict):
                event = dict(event)
        except Exception as exc:  # SignatureVerificationError etc.
            log.warning("stripe signature verification failed: %s", exc)
            return JSONResponse(
                {"error": "Invalid signature"}, status_code=400,
            )
    else:
        # No secret configured. We deliberately do NOT fall back to
        # parsing the JSON unsigned — that re-opens the subscription
        # forgery hole the stub warned about.
        log.warning("STRIPE_WEBHOOK_SECRET not set — refusing webhook")
        return JSONResponse(
            {"error": "Webhook signing not configured"},
            status_code=503,
        )

    # 6) Live-mode gate.
    if event.get("livemode") and not _stripe_live_mode_enabled():
        log.warning(
            "rejecting livemode=True event in non-live env: type=%s id=%s",
            event.get("type"), event.get("id"),
        )
        return JSONResponse(
            {"error": "Live events not accepted in this environment"},
            status_code=400,
        )

    # 7) Idempotency.
    replayed = mark_received(event)
    if replayed is not None:
        return replayed

    # 8) Dispatch.
    event_type = event.get("type") or ""
    error_msg: Optional[str] = None
    try:
        if event_type == "customer.subscription.created":
            _grant_access(event)
        elif event_type == "customer.subscription.updated":
            _update_plan(event)
        elif event_type == "customer.subscription.deleted":
            apply_subscription_cancelled(event)
        elif event_type == "invoice.payment_failed":
            apply_invoice_payment_failed(event)
        elif event_type == "invoice.paid":
            _record_payment(event)
        else:
            log.debug(
                "ignoring stripe event type=%s id=%s",
                event_type, event.get("id"),
            )
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)[:500]
        log.exception(
            "stripe handler raised: type=%s id=%s",
            event_type, event.get("id"),
        )

    mark_processed(event, error=error_msg)
    return JSONResponse({"status": "ok"})
