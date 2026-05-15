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
  6. Idempotency precheck — read-only existence query on the
     ``processed_stripe_events`` ledger. Row present iff a prior
     delivery already succeeded → short-circuit with 200
     ``already_processed``. Audit MED FIX
     (audit_stripe_idempotent.md GAP-3): the precheck does NOT
     write the ledger row anymore — the write was hoisted to AFTER
     successful dispatch so a crash mid-dispatch leaves no row and
     Stripe's retry re-runs the dispatch branch fresh.
  7. Dispatch — branch on ``event["type"]``; wrapped in try/except
     so a broken branch can't take down the route. On crash, we
     ``log.exception`` and respond 200 with NO ledger row written —
     the missing row is the retry signal for the next Stripe delivery.
  8. Post-dispatch (success only) — ``mark_received`` + ``mark_processed``
     write the ledger row and stamp ``processed_at`` in a single
     post-success step. Concurrent retries can both run side effects
     before either writes the row; the audit doc accepts this trade-off
     ("sacrificing crash-in-flight idempotency for crash-survivability")
     because Stripe spaces retries minutes apart and the dispatch
     branches all have UPSERT / status-flip semantics that collapse
     true duplicates.
  9. Always 200 on accepted events. The four reject cases above use
     403/400/503/429. Stripe retries non-2xx for 3 days so we'd
     rather log and respond 200 than amplify retries.

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
    reject_mode_mismatch,
    reject_non_stripe_ip,
)


log = logging.getLogger("stripe.webhook")


def _stripe_live_mode_enabled() -> bool:
    """``STRIPE_LIVE_MODE=true`` (or 1/yes/on) opts in to accepting
    ``livemode=True`` Stripe events. Default false so a fresh dev box
    cannot accidentally honour production webhooks."""
    flag = os.environ.get("STRIPE_LIVE_MODE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _is_production_env() -> bool:
    """Mirror of stripe_webhook_hardening._is_production.

    Kept here to avoid reaching into the hardening module's private
    helper. ``PRODUCTION=1`` is the only canonical production signal
    the gateway respects.
    """
    return os.environ.get("PRODUCTION", "0") == "1"


def _trust_user_from_metadata(
    user_id: Optional[int],
    customer_id: str,
) -> Optional[int]:
    """Fix E: return user_id only if it matches the customer-id mapping.

    Audit finding (HIGH): metadata.user_id was trusted blindly. An
    attacker who creates a Stripe customer with their own card and sets
    metadata.user_id=42 in the checkout session could grant a paid
    subscription to user 42. The fix is to require that the customer
    on the event maps to the same user_id we already have on file in
    ``users.stripe_customer_id`` (set by the first
    ``checkout.session.completed`` for that customer).

    Returns the trusted user_id, or None if the metadata-customer pair
    is inconsistent. If no row maps to customer_id yet (first event),
    we accept the metadata value -- that matches the canonical
    first-touch flow from checkout.session.completed.
    """
    if not customer_id or user_id is None:
        return user_id
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE stripe_customer_id = ?",
                (customer_id,),
            ).fetchone()
    except Exception as exc:
        log.warning(
            "stripe metadata trust check DB read failed (cust=%s): %s",
            customer_id, exc,
        )
        return user_id
    if row is None:
        return user_id
    if int(row["id"]) != int(user_id):
        log.warning(
            "stripe metadata user_id mismatch: metadata=%s mapped=%s cust=%s",
            user_id, row["id"], customer_id,
        )
        return None
    return user_id


def _stripe_webhook_secret() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def _coerce_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bust_user_caches(user_id: int) -> None:
    """Tier-change cache bust for the positive-direction webhook branches.

    AUDIT (CRIT, audit_tier_change.md): the cancellation / payment-failed
    branches in ``stripe_webhook_hardening`` already invalidate both the
    sync TTL cache (via ``ttl_invalidate.on_subscription_change``) and
    the subproduct access verdict cache (via
    ``subproduct_access.invalidate_user``). The positive branches
    (``subscription.created`` / ``updated`` / ``invoice.paid``) did
    neither, so a Stripe-driven upgrade left a paying user staring at
    locked dashboards for 60s and a 402 from the subproduct gate for
    5min. This helper centralises the bust so all three positive
    branches share the same contract as the negative branches.
    """
    if not user_id:
        return
    try:
        from cache import ttl_invalidate
        ttl_invalidate.on_subscription_change(user_id)
    except Exception:
        log.exception(
            "ttl_invalidate.on_subscription_change failed (user=%s)", user_id,
        )
    try:
        from subproduct_access import invalidate_user
        invalidate_user(user_id)
    except Exception:
        log.exception(
            "subproduct_access.invalidate_user failed (user=%s)", user_id,
        )


def _user_id_for_stripe_sub(sub_id: str) -> Optional[int]:
    """Resolve a ``subscriptions.stripe_sub_id`` back to its user_id.

    Used by ``_record_payment`` (invoice.paid) — Stripe's invoice object
    carries the subscription id but no ``metadata.user_id``, so we have
    to look up the local row to know whose caches to invalidate.
    """
    if not sub_id:
        return None
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT user_id FROM subscriptions WHERE stripe_sub_id = ? LIMIT 1",
                (sub_id,),
            ).fetchone()
        if row is None:
            return None
        return int(row["user_id"])
    except Exception as exc:
        log.warning("user_id lookup for stripe_sub_id=%s failed: %s", sub_id, exc)
        return None


# ── Dispatch branches ──────────────────────────────────────────────────────


def _grant_access(event: dict) -> None:
    """customer.subscription.created — write the subscriptions row.

    Metadata contract: ``user_id`` + ``dashboard_key`` (or alias
    ``subproduct_slug``); ``plan`` optional. UPSERT on the
    (user_id, dashboard_key) unique key so a misordered Stripe event
    sequence doesn't crash on duplicates.

    Audit MED-1 (queries/billing): we MUST persist
    ``current_period_end`` as ``subscriptions.expires_at`` (Unix epoch
    seconds, stored in the INTEGER column). Skipping it left the row at
    NULL, which the access checks historically treated as "no expiry" —
    so a missed ``customer.subscription.deleted`` event left the user
    paid-up forever. ``has_active_subscription`` now fails closed on
    NULL, but the webhook is the only writer that can populate the
    value in the first place.
    """
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    raw_user_id = _coerce_int(meta.get("user_id") or meta.get("narve_user_id"))
    customer_id = (obj.get("customer") or "").strip()
    user_id = _trust_user_from_metadata(raw_user_id, customer_id)
    dashboard_key = (
        meta.get("dashboard_key") or meta.get("subproduct_slug") or ""
    ).strip()
    plan = (meta.get("plan") or "default").strip() or "default"
    stripe_sub_id = obj.get("id") or ""
    expires_at = _coerce_int(obj.get("current_period_end"))

    if not user_id or not dashboard_key:
        log.warning(
            "subscription.created missing/untrusted metadata: "
            "raw_user_id=%s mapped=%s key=%s cust=%s id=%s",
            raw_user_id, user_id, dashboard_key, customer_id, event.get("id"),
        )
        return

    if expires_at is None:
        # Stripe always sends current_period_end on subscription.created;
        # a missing value is a malformed event. Stamp a short fallback
        # window so the row fails closed soon rather than living forever.
        log.warning(
            "subscription.created missing current_period_end: user_id=%s "
            "key=%s id=%s — defaulting expires_at to now+1h",
            user_id, dashboard_key, event.get("id"),
        )
        expires_at = int(time.time()) + 3600

    now = int(time.time())
    try:
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions "
                "(user_id, dashboard_key, plan, status, started_at, "
                " expires_at, stripe_sub_id, source) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?, 'stripe') "
                "ON CONFLICT(user_id, dashboard_key) DO UPDATE SET "
                "  plan = excluded.plan, "
                "  status = 'active', "
                "  expires_at = excluded.expires_at, "
                "  stripe_sub_id = excluded.stripe_sub_id, "
                "  source = 'stripe'",
                (user_id, dashboard_key, plan, now, expires_at, stripe_sub_id),
            )
    except Exception as exc:
        log.warning("grant_access DB write failed: %s", exc)

    # AUDIT (CRIT, audit_tier_change.md): bust the per-user sync TTL
    # cache + subproduct access verdict cache so the very next request
    # observes the new active subscription. Negative-direction branches
    # already do this; without it the dashboard remains stale 60s and
    # the subproduct gate stays stale up to 5min.
    _bust_user_caches(user_id)


def _update_plan(event: dict) -> None:
    """customer.subscription.updated — sync plan + status from Stripe.

    Collapses any non-active Stripe lifecycle state ("incomplete",
    "past_due", "unpaid", etc.) to ``inactive`` locally — the UI only
    needs to know "user has it" vs "user doesn't".

    Audit MED-1: always refresh ``expires_at`` from
    ``current_period_end`` so renewals extend the window and downgrades
    shorten it. A NULL ``current_period_end`` would normally only land
    on malformed events; we leave the existing value alone in that case
    rather than blank it (which would now flip the row to "expired"
    under the closed-fail rule)."""
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    raw_user_id = _coerce_int(meta.get("user_id") or meta.get("narve_user_id"))
    customer_id = (obj.get("customer") or "").strip()
    user_id = _trust_user_from_metadata(raw_user_id, customer_id)
    dashboard_key = (
        meta.get("dashboard_key") or meta.get("subproduct_slug") or ""
    ).strip()
    plan = (meta.get("plan") or "").strip()
    stripe_status = (obj.get("status") or "active").strip()
    local_status = "active" if stripe_status in {"active", "trialing"} else "inactive"
    expires_at = _coerce_int(obj.get("current_period_end"))

    if not user_id or not dashboard_key:
        log.warning(
            "subscription.updated missing/untrusted metadata: "
            "raw_user_id=%s mapped=%s key=%s cust=%s id=%s",
            raw_user_id, user_id, dashboard_key, customer_id, event.get("id"),
        )
        return

    try:
        with db.conn() as c:
            params: list = [local_status]
            sets = ["status = ?"]
            if plan:
                sets.append("plan = ?")
                params.append(plan)
            if expires_at is not None:
                sets.append("expires_at = ?")
                params.append(expires_at)
            params.extend([user_id, dashboard_key])
            c.execute(
                f"UPDATE subscriptions SET {', '.join(sets)} "
                f"WHERE user_id = ? AND dashboard_key = ?",
                params,
            )
    except Exception as exc:
        log.warning("update_plan DB write failed: %s", exc)

    # AUDIT (CRIT, audit_tier_change.md): same as _grant_access — any
    # subscription.updated flip (active↔past_due, plan switch, renewal
    # extension) is a tier-change observable in the per-user feed and
    # the subproduct access verdict, so the bust fires here too.
    _bust_user_caches(user_id)


def _record_payment(event: dict) -> None:
    """invoice.paid — flip a past_due subscription back to active and
    extend the access window.

    Audit MED-1: a renewal payment is the moment we know the new
    ``current_period_end`` for the subscription. The invoice object
    carries it on each line item as ``lines.data[].period.end``; we
    pick the latest such value as the row's new ``expires_at``. Without
    this, a renewing customer's ``expires_at`` froze at the original
    period and the row flipped to "expired" mid-cycle under the
    closed-fail rule introduced in MED-1.
    """
    obj = (event.get("data") or {}).get("object") or {}
    customer = obj.get("customer") or ""
    sub_id = obj.get("subscription") or ""
    if not sub_id:
        return

    # Stripe invoices may carry the period on the invoice itself, or
    # nested per-line. Pick the latest non-null we find so a multi-line
    # invoice (proration + base) lands on the longest window.
    new_expires_at: Optional[int] = None
    candidate = _coerce_int(obj.get("period_end"))
    if candidate is not None:
        new_expires_at = candidate
    lines = (obj.get("lines") or {}).get("data") or []
    for line in lines:
        period = line.get("period") or {}
        cand = _coerce_int(period.get("end"))
        if cand is not None and (new_expires_at is None or cand > new_expires_at):
            new_expires_at = cand

    try:
        with db.conn() as c:
            params: list = []
            sets = ["status = 'active'"]
            if new_expires_at is not None:
                sets.append("expires_at = ?")
                params.append(new_expires_at)
            params.append(sub_id)
            # past_due + active both get the refreshed window. Stripe
            # sends invoice.paid for normal renewals, not just dunning
            # recoveries, so a healthy active row also needs the bump.
            c.execute(
                f"UPDATE subscriptions SET {', '.join(sets)} "
                f"WHERE stripe_sub_id = ? AND status IN ('active', 'past_due')",
                params,
            )
    except Exception as exc:
        log.warning(
            "record_payment DB write failed: %s (sub=%s cust=%s)",
            exc, sub_id, customer,
        )

    # AUDIT (CRIT, audit_tier_change.md): the invoice.paid path is how
    # a past_due subscription returns to active — i.e. the user regains
    # access. The Stripe invoice carries the subscription id but no
    # metadata.user_id, so we map sub_id → user_id via the local row
    # and then run the standard tier-change bust.
    user_id = _user_id_for_stripe_sub(sub_id)
    if user_id is not None:
        _bust_user_caches(user_id)





def _link_customer(event: dict) -> None:
    """checkout.session.completed - persist users.stripe_customer_id (Fix E).

    Audit finding: metadata.user_id was trusted blindly on every
    subscription event. This handler is where we first see a customer
    id paired with a freshly-authenticated narve user, so we write the
    mapping here. After this, _trust_user_from_metadata rejects any
    subsequent event whose customer id maps to a different user.
    """
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or (obj.get("subscription_data") or {}).get("metadata") or {}
    raw_user_id = _coerce_int(meta.get("user_id") or meta.get("narve_user_id"))
    customer_id = (obj.get("customer") or "").strip()
    event_id = event.get("id")

    if not raw_user_id or not customer_id:
        log.info(
            "checkout.session.completed missing user_id/customer: "
            "user_id=%s cust=%s id=%s - nothing to link",
            raw_user_id, customer_id, event_id,
        )
        return

    try:
        with db.conn() as c:
            existing = c.execute(
                "SELECT id, stripe_customer_id FROM users WHERE id = ?",
                (raw_user_id,),
            ).fetchone()
            if existing is None:
                log.warning(
                    "checkout.session.completed: user_id=%s not found, "
                    "refusing to link customer=%s id=%s",
                    raw_user_id, customer_id, event_id,
                )
                return
            if existing["stripe_customer_id"]:
                if existing["stripe_customer_id"] != customer_id:
                    log.warning(
                        "checkout.session.completed: user_id=%s already "
                        "mapped to cust=%s, refusing to overwrite with %s "
                        "(event_id=%s)",
                        raw_user_id, existing["stripe_customer_id"],
                        customer_id, event_id,
                    )
                return
            other = c.execute(
                "SELECT id FROM users WHERE stripe_customer_id = ?",
                (customer_id,),
            ).fetchone()
            if other is not None and int(other["id"]) != int(raw_user_id):
                log.warning(
                    "checkout.session.completed: cust=%s already mapped to "
                    "user_id=%s, refusing to re-bind to %s (event_id=%s)",
                    customer_id, other["id"], raw_user_id, event_id,
                )
                return
            c.execute(
                "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
                (customer_id, raw_user_id),
            )
    except Exception as exc:
        log.warning(
            "link_customer DB write failed: %s (user=%s cust=%s)",
            exc, raw_user_id, customer_id,
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

    # 6) Live-mode gate -- symmetric (Fix E).
    #
    # Audit finding: the previous gate only rejected livemode=True
    # events in non-live envs. The reverse hole (livemode=False test
    # events accepted in production) let an attacker who guessed the
    # webhook URL forward signed test-account events into prod.
    # reject_mode_mismatch enforces both directions: production accepts
    # only livemode=True, non-prod accepts only livemode=False.
    #
    # The bespoke _stripe_live_mode_enabled() env-flag check is kept
    # for non-prod envs (PRODUCTION!=1) where we still want a narrow
    # escape hatch (STRIPE_LIVE_MODE=true) for staging-against-real-
    # Stripe. Production gets no such opt-out.
    if _is_production_env():
        mode_reject = reject_mode_mismatch(event)
        if mode_reject is not None:
            return mode_reject
    else:
        if event.get("livemode") and not _stripe_live_mode_enabled():
            log.warning(
                "rejecting livemode=True event in non-live env: type=%s id=%s",
                event.get("type"), event.get("id"),
            )
            return JSONResponse(
                {"error": "Live events not accepted in this environment"},
                status_code=400,
            )
        elif event.get("livemode") is None:
            log.warning(
                "rejecting event with missing livemode flag: type=%s id=%s",
                event.get("type"), event.get("id"),
            )
            return JSONResponse(
                {"error": "Malformed event: livemode flag required"},
                status_code=400,
            )

    # 7) Idempotency — read-only check before dispatch.
    #
    # Audit MED FIX (audit_stripe_idempotent.md GAP-3 closure): write
    # the ledger row AFTER successful dispatch, not before. Previously
    # the route called ``mark_received`` first (which INSERTs the row),
    # then dispatched, then called ``mark_processed`` on BOTH success
    # and failure. A crash mid-dispatch left the row stamped with
    # ``processed_at``, so Stripe's retry short-circuited as
    # ``already_processed`` and the post-crash side effects were
    # permanently lost.
    #
    # The new flow is approach (b) from the audit doc literally —
    # "move the ``mark_received`` write to AFTER the dispatch branch
    # succeeds." Concretely:
    #
    #   * Pre-dispatch: read-only existence check on the ledger.
    #     If a row exists, short-circuit as ``already_processed``.
    #     A row exists iff a PRIOR delivery succeeded — crashed
    #     attempts never write because the INSERT was hoisted past
    #     the dispatch.
    #   * Dispatch: runs as before. On crash we ``log.exception`` and
    #     respond 200 so Stripe stops retrying THIS delivery — the next
    #     retry will find no ledger row and run dispatch fresh.
    #   * Post-dispatch (success): ``mark_received`` writes the row,
    #     ``mark_processed`` stamps ``processed_at``. Concurrent retries
    #     hitting the dispatch branch simultaneously both run side
    #     effects — this is the trade-off the audit doc calls out
    #     explicitly ("sacrificing crash-in-flight idempotency for
    #     crash-survivability"). Stripe spaces retries minutes apart,
    #     so the race window is theoretical for real traffic and the
    #     UPSERT defence on ``subscriptions`` collapses true duplicates
    #     to a single row anyway.
    event_id = event.get("id") or ""
    if event_id:
        try:
            with db.conn() as c:
                row = c.execute(
                    "SELECT 1 FROM processed_stripe_events "
                    "WHERE event_id = ? LIMIT 1",
                    (event_id,),
                ).fetchone()
            if row is not None:
                return JSONResponse({"status": "already_processed"})
        except Exception as exc:
            # Ledger unreachable → fall through and dispatch. Same
            # fail-open shape ``mark_received`` had — we'd rather risk a
            # duplicate dispatch than miss the event entirely on a
            # transient DB hiccup. The dispatch branches all have UPSERT
            # / status-flip semantics that collapse duplicates.
            log.warning(
                "stripe idempotency precheck failed (%s): %s — "
                "proceeding with dispatch",
                event_id, exc,
            )

    # 8) Dispatch.
    event_type = event.get("type") or ""
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
        elif event_type == "checkout.session.completed":
            _link_customer(event)
        else:
            log.debug(
                "ignoring stripe event type=%s id=%s",
                event_type, event.get("id"),
            )
    except Exception:  # noqa: BLE001
        # Crash mid-dispatch. The ledger row was NEVER written, so
        # Stripe's retry will re-enter the dispatch branch fresh on the
        # next delivery (no short-circuit). The per-attempt error text
        # is captured in the log.exception trace; the admin panel sees
        # only the FINAL ledger row (which only lands on a successful
        # dispatch).
        log.exception(
            "stripe handler raised: type=%s id=%s",
            event_type, event.get("id"),
        )
        # Respond 200 so Stripe doesn't enter a retry storm on top of
        # the application-level retry. The MISSING ledger row IS the
        # retry signal — Stripe's exponential-backoff redelivery
        # remains in effect for 3 days.
        return JSONResponse({"status": "ok"})

    # Success: write the ledger row + stamp processed_at. This is the
    # ONLY place ``mark_received`` runs, so the row only ever exists
    # for events that fully dispatched without raising.
    mark_received(event)
    mark_processed(event)
    return JSONResponse({"status": "ok"})
