"""Outbound webhook delivery — HMAC-signed, retrying, with auto-disable.

Two entry points:

  ``broadcast_event(event_type, payload)`` — call this from anywhere in the
    app to fan out an event to every active subscription that listens for
    it. Thin wrapper around _deliver() + httpx; non-blocking for the caller.

  ``hub_bridge(channel, message)`` — plug-in that maps realtime hub
    channels to webhook event types. Registered from server.py startup
    so every hub.broadcast() also reaches external subscribers.

Retry schedule: 30s → 5min → 30min → 1h → 4h, then mark failed. Five
consecutive failed deliveries (across any event) auto-deactivate the
subscription and enqueue a `webhook_disabled` email to the owner.

Signatures: X-Narve-Signature = hex(HMAC-SHA256(subscription.secret,
raw_body)). The raw body is the UTF-8 JSON string with separators
``(",",":")`` so consumers can re-sign bit-for-bit.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional

import httpx

import db


log = logging.getLogger("webhooks")


# Event taxonomy. Keys are the event_type strings we emit; values are the
# realtime-hub channels that should also trigger this event (if the hub is
# in use). Subscribers pick any subset of these in their `events` array.
EVENT_TYPES: tuple[str, ...] = (
    "best_bet.new",
    "market.resolved",
    "source.credibility_updated",
    "insider_signal.new",
    "user.prediction.resolved",
)

# Cumulative retry schedule (seconds). Each index = attempt number - 1.
RETRY_DELAYS: tuple[int, ...] = (30, 5 * 60, 30 * 60, 60 * 60, 4 * 60 * 60)

# After this many consecutive failures we disable the subscription.
AUTO_DISABLE_AFTER = 5

# HTTP timeout per delivery attempt.
DELIVERY_TIMEOUT_S = 10.0

# JSON separators tuned so consumers who re-sign get byte-identical input.
_JSON_ARGS = {"separators": (",", ":"), "sort_keys": True, "default": str}


# ── Core delivery ───────────────────────────────────────────────────────


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _deliver_once(
    *,
    webhook_id: int,
    url: str,
    secret: str,
    event_type: str,
    body_bytes: bytes,
    attempt: int,
) -> tuple[Optional[int], Optional[str]]:
    """POST the payload once. Returns (status_code, error_string).

    Wraps any httpx exception in a readable error string so the delivery
    log stays useful — we never let an exception bubble out of here.
    """
    signature = _sign(secret, body_bytes)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "narve-webhooks/1",
        "X-Narve-Event": event_type,
        "X-Narve-Delivery": f"wh_{webhook_id}_{int(time.time()*1000)}_{attempt}",
        "X-Narve-Signature": f"sha256={signature}",
    }
    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_S) as client:
            resp = await client.post(url, content=body_bytes, headers=headers)
        return resp.status_code, None
    except httpx.TimeoutException:
        return None, "timeout"
    except httpx.HTTPError as exc:
        return None, f"http_error: {exc.__class__.__name__}: {exc}"[:400]
    except Exception as exc:  # pragma: no cover — last-ditch
        return None, f"unknown: {exc.__class__.__name__}: {exc}"[:400]


async def _deliver_with_retries(
    *,
    webhook_id: int,
    url: str,
    secret: str,
    event_type: str,
    payload: dict,
    max_attempts: Optional[int] = None,
) -> bool:
    """Attempt delivery with exponential backoff; return True if any attempt
    got a 2xx. Logs every attempt as a row in webhook_deliveries.

    Side-effects on success: resets consecutive_failures, updates
    last_delivered_at. Side-effects on final failure: increments
    consecutive_failures; if that hits AUTO_DISABLE_AFTER, flips
    is_active to 0 and enqueues the warning email.
    """
    body_bytes = json.dumps(payload, **_JSON_ARGS).encode()
    attempts_cap = max_attempts or len(RETRY_DELAYS)
    last_status: Optional[int] = None
    last_error: Optional[str] = None

    for attempt in range(1, attempts_cap + 1):
        status, error = await _deliver_once(
            webhook_id=webhook_id, url=url, secret=secret,
            event_type=event_type, body_bytes=body_bytes, attempt=attempt,
        )
        last_status, last_error = status, error

        # Log EVERY attempt so the admin UI can show the full retry trail.
        try:
            db.record_webhook_delivery(
                webhook_id=webhook_id, event_type=event_type,
                payload=body_bytes.decode(errors="replace"),
                status_code=status, attempts=attempt, error=error,
            )
        except Exception:
            pass  # delivery log is best-effort — don't block retries on it

        if status is not None and 200 <= status < 300:
            try:
                db.reset_webhook_failure(webhook_id)
            except Exception:
                pass
            return True

        if attempt < attempts_cap:
            await asyncio.sleep(RETRY_DELAYS[attempt - 1])

    # All attempts exhausted → bump + maybe deactivate.
    try:
        consecutive = db.bump_webhook_failure(webhook_id)
    except Exception:
        consecutive = 0
    log.warning(
        "webhook %s failed event=%s attempts=%d consecutive=%d last=%s err=%s",
        webhook_id, event_type, attempts_cap, consecutive,
        last_status, last_error,
    )
    if consecutive >= AUTO_DISABLE_AFTER:
        try:
            db.deactivate_webhook(webhook_id)
        except Exception:
            pass
        try:
            _enqueue_disabled_email(webhook_id, consecutive)
        except Exception:
            pass
    return False


def _enqueue_disabled_email(webhook_id: int, consecutive: int) -> None:
    """Email the owner that we turned their webhook off.

    Wrapped in its own helper so it can be stubbed in tests. Uses the
    background jobs registry if available; falls back to direct send.
    """
    try:
        sub = db.get_webhook_subscription(webhook_id)
        if not sub:
            return
        owner = db.get_user_by_id(sub["user_id"])
        if not owner:
            return
    except Exception:
        return

    ctx = {
        "display_name": owner["username"] if "username" in owner.keys() else owner["email"],
        "webhook_url": sub["url"],
        "consecutive_failures": consecutive,
    }
    try:
        # Preferred: enqueue via ARQ / in-process worker.
        import jobs
        if hasattr(jobs, "enqueue_email"):
            jobs.enqueue_email(
                to=owner["email"],
                template="webhook_disabled",
                context=ctx,
            )
            return
    except Exception:
        pass
    # Fallback: fire-and-forget send, so the message doesn't rot in queue.
    try:
        from email_system.service import EmailService
        svc = EmailService()
        import asyncio as _a
        _a.get_event_loop().create_task(
            svc.send_template(owner["email"], "webhook_disabled", ctx)
        )
    except Exception as exc:
        log.warning("could not email webhook_disabled for wh=%s: %s", webhook_id, exc)


# ── Public API ──────────────────────────────────────────────────────────


async def broadcast_event(event_type: str, payload: dict) -> int:
    """Fan *event_type* out to every active subscription listening for it.

    Returns the number of subscriptions dispatched to (not necessarily
    succeeded). Delivery is gathered with asyncio.gather so one slow
    subscriber never blocks another.
    """
    if event_type not in EVENT_TYPES:
        log.debug("broadcast_event: unknown event_type=%s — ignoring", event_type)
        return 0
    try:
        subs = db.list_active_webhooks_for_event(event_type)
    except Exception as exc:
        log.warning("list_active_webhooks_for_event failed: %s", exc)
        return 0
    if not subs:
        return 0

    envelope = {
        "event": event_type,
        "delivered_at": int(time.time()),
        "data": payload,
    }

    async def _one(sub):
        try:
            return await _deliver_with_retries(
                webhook_id=sub["id"], url=sub["url"],
                secret=sub["secret"], event_type=event_type,
                payload=envelope,
            )
        except Exception as exc:
            log.exception("webhook %s delivery crashed: %s", sub["id"], exc)
            return False

    await asyncio.gather(*[_one(s) for s in subs], return_exceptions=True)
    return len(subs)


# ── Hub bridge ──────────────────────────────────────────────────────────
#
# Maps realtime-hub channels to webhook event types. The hub calls
# ``hub_bridge(channel, message)`` after its own broadcast completes, and
# if the channel matches we also emit the public event. Keeps the hub
# decoupled from webhook state.

_CHANNEL_TO_EVENT: dict[str, str] = {
    "best_bets": "best_bet.new",
    "market_resolutions": "market.resolved",
    "source_credibility": "source.credibility_updated",
    "insider_signals": "insider_signal.new",
    "user_prediction_resolutions": "user.prediction.resolved",
}


async def hub_bridge(channel: str, message: dict) -> None:
    event_type = _CHANNEL_TO_EVENT.get(channel)
    if event_type is None:
        return
    try:
        await broadcast_event(event_type, message)
    except Exception as exc:
        log.warning("hub_bridge %s → %s failed: %s", channel, event_type, exc)


def register_with_hub() -> None:
    """Optional: hook us into the existing realtime hub so every internal
    broadcast also fans out to external webhook subscribers. No-op if the
    hub module isn't present or doesn't expose the expected API.
    """
    try:
        from realtime import hub as _hub
    except Exception:
        log.info("webhooks: realtime.hub not available — external fan-out inactive")
        return
    if not hasattr(_hub, "register_after_broadcast"):
        log.info("webhooks: hub lacks register_after_broadcast — skipping bridge")
        return
    try:
        _hub.register_after_broadcast(hub_bridge)
        log.info("webhooks: bridged into realtime hub")
    except Exception as exc:
        log.warning("webhooks: hub register failed: %s", exc)


# ── Synchronous test helper ─────────────────────────────────────────────


async def fire_test_payload(webhook_id: int) -> dict:
    """Used by POST /settings/webhooks/{id}/test — send a synthetic
    ``test.ping`` event so the owner can verify their endpoint without
    waiting for a real event to land."""
    sub = db.get_webhook_subscription(webhook_id)
    if not sub:
        return {"ok": False, "error": "webhook not found"}
    payload = {
        "event": "test.ping",
        "delivered_at": int(time.time()),
        "data": {
            "message": "This is a test delivery from narve.ai.",
            "webhook_id": webhook_id,
        },
    }
    body_bytes = json.dumps(payload, **_JSON_ARGS).encode()
    status, err = await _deliver_once(
        webhook_id=webhook_id, url=sub["url"],
        secret=sub["secret"], event_type="test.ping",
        body_bytes=body_bytes, attempt=1,
    )
    try:
        db.record_webhook_delivery(
            webhook_id=webhook_id, event_type="test.ping",
            payload=body_bytes.decode(errors="replace"),
            status_code=status, attempts=1, error=err,
        )
    except Exception:
        pass
    return {"ok": status is not None and 200 <= status < 300,
            "status_code": status, "error": err}
