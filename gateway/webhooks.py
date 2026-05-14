"""Outbound webhook delivery — HMAC-signed, retrying, circuit-broken.

Two entry points:

  ``broadcast_event(event_type, payload)`` — call this from anywhere in the
    app to fan out an event to every active subscription that listens for
    it. Thin wrapper around _deliver() + httpx; non-blocking for the caller.

  ``hub_bridge(channel, message)`` — plug-in that maps realtime hub
    channels to webhook event types. Registered from server.py startup
    so every hub.broadcast() also reaches external subscribers.

Hardening contract (post-2026-05-14):

  * **Retry policy.** Three attempts with exponential backoff (2s/4s/8s).
    Retried on 5xx responses, connection errors, and timeouts; **not**
    retried on 4xx (the subscriber's URL is broken — bouncing five more
    times won't fix it). Retries are intra-call only — no cross-call
    queue.

  * **Dead-letter queue.** A delivery that burns through every attempt
    without a 2xx is written to ``webhook_dead_letter`` so the admin
    panel at ``/admin/webhooks/dead-letter`` can show stuck deliveries
    and re-queue them on the owner's request.

  * **Circuit breaker.** Ten consecutive failures (across any event)
    stamps ``disabled_until = now + 1h`` on the subscription and emails
    the owner. The fan-out loop refuses to send while the breaker is
    open. After the cooldown elapses, the next event triggers a probe
    delivery; success resets the counter and re-arms the subscription.

  * **HMAC signature.** ``X-Narve-Signature: sha256=<hex>`` where the
    HMAC input is the **timestamp + "." + raw_body** (newline-free).
    Including the timestamp in the signed payload defeats a MITM that
    strips/replaces the timestamp header to slip past anti-replay.

  * **Anti-replay timestamp.** ``X-Narve-Timestamp: <unix>`` accompanies
    every delivery. Receivers should reject anything older than 5
    minutes (``REPLAY_WINDOW_S``) to defeat captured-and-resent attacks.
    ``verify_signature()`` below implements the receiver-side check so
    consumers writing in-process tests have a canonical reference.

Signatures: X-Narve-Signature = hex(HMAC-SHA256(subscription.secret,
"<timestamp>.<raw_body>")). The raw body is the UTF-8 JSON string with
separators ``(",",":")`` so consumers can re-sign bit-for-bit.
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

# Intra-call retry schedule. Three attempts means two sleeps between them
# (post-attempt-1 and post-attempt-2). Values are seconds — exponential 2/4/8.
# Indexed by ``attempt - 1``; the final attempt's slot is never read.
RETRY_DELAYS: tuple[int, ...] = (2, 4, 8)
MAX_ATTEMPTS = 3

# Circuit-breaker: after this many consecutive failures, we open the
# breaker (stamp disabled_until) and notify the owner.
CIRCUIT_BREAKER_THRESHOLD = 10

# Cooldown — how long the breaker stays open before the next event is
# allowed through as a probe.
CIRCUIT_BREAKER_COOLDOWN_S = 60 * 60  # 1 hour

# HTTP timeout per delivery attempt.
DELIVERY_TIMEOUT_S = 10.0

# Receiver-side anti-replay tolerance. Consumers reject deliveries whose
# X-Narve-Timestamp is more than this many seconds in the past or future.
REPLAY_WINDOW_S = 5 * 60

# JSON separators tuned so consumers who re-sign get byte-identical input.
_JSON_ARGS = {"separators": (",", ":"), "sort_keys": True, "default": str}


# ── Signing ─────────────────────────────────────────────────────────────


def _sign(secret: str, body: bytes, *, timestamp: Optional[int] = None) -> str:
    """Return the hex HMAC-SHA256.

    If ``timestamp`` is given, the input is ``"<ts>.<body>"`` (signs the
    timestamp alongside the body so a MITM can't swap headers). If
    omitted, signs the body alone — this two-mode shape lets the legacy
    test path (which signs just the body) keep working.
    """
    if timestamp is None:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    signed = str(timestamp).encode() + b"." + body
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def verify_signature(
    *,
    secret: str,
    body: bytes,
    signature_header: str,
    timestamp_header: str,
    now: Optional[int] = None,
    tolerance_s: int = REPLAY_WINDOW_S,
) -> bool:
    """Receiver-side helper — verify an incoming webhook.

    Returns True iff:
      * the timestamp parses as an int,
      * the timestamp is within ``tolerance_s`` of ``now`` (defaults to
        wall clock — replay protection),
      * the signature header is in the canonical ``sha256=<hex>`` form,
      * the recomputed HMAC matches in constant time.

    Consumers writing their own verifier can use this as a reference.
    Tests use it directly to assert the produced headers round-trip.
    """
    try:
        ts = int(timestamp_header)
    except (TypeError, ValueError):
        return False
    now = int(time.time()) if now is None else int(now)
    if abs(now - ts) > tolerance_s:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = _sign(secret, body, timestamp=ts)
    return hmac.compare_digest(expected, signature_header[len("sha256="):])


# ── Core delivery ───────────────────────────────────────────────────────


async def _deliver_once(
    *,
    webhook_id: int,
    url: str,
    secret: str,
    event_type: str,
    body_bytes: bytes,
    attempt: int,
    timestamp: Optional[int] = None,
) -> tuple[Optional[int], Optional[str]]:
    """POST the payload once. Returns (status_code, error_string).

    Wraps any httpx exception in a readable error string so the delivery
    log stays useful — we never let an exception bubble out of here.
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    # Sign timestamp+body so the anti-replay header is itself authenticated.
    signature = _sign(secret, body_bytes, timestamp=ts)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "narve-webhooks/1",
        "X-Narve-Event": event_type,
        "X-Narve-Delivery": f"wh_{webhook_id}_{ts}_{attempt}",
        "X-Narve-Timestamp": str(ts),
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


def _is_retryable(status: Optional[int], error: Optional[str]) -> bool:
    """Decide whether an attempt result is worth retrying.

    Retry on:
      * 5xx (server is having a moment)
      * connection / timeout errors (status is None, error is set)

    Don't retry on:
      * 2xx (we're done — caller treats this as success)
      * 4xx (user's URL/auth is broken — five more 404s won't help)
    """
    if status is None:
        # Connection-level failure — retry.
        return error is not None
    if 200 <= status < 300:
        return False  # success — caller short-circuits before checking us
    if 400 <= status < 500:
        return False  # client error — terminal
    return True  # 5xx and anything else weird — retry


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

    Side-effects on success: resets consecutive_failures and disabled_until,
    updates last_delivered_at. Side-effects on terminal failure: moves the
    payload to ``webhook_dead_letter``, bumps consecutive_failures, and
    opens the circuit breaker if the threshold is hit.

    Returns False on terminal failure (whether reached by exhausting
    retries or by a non-retryable 4xx).
    """
    body_bytes = json.dumps(payload, **_JSON_ARGS).encode()
    attempts_cap = max_attempts or MAX_ATTEMPTS
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    first_failed_at: Optional[int] = None

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

        # Record when this delivery first started failing (used by DLQ).
        if first_failed_at is None:
            first_failed_at = int(time.time())

        # Non-retryable response → straight to DLQ without further attempts.
        if not _is_retryable(status, error):
            _drop_to_dlq(
                webhook_id=webhook_id, event_type=event_type,
                body_bytes=body_bytes, last_error=_compose_err(status, error),
                attempts=attempt, first_failed_at=first_failed_at,
            )
            _bump_and_maybe_break(
                webhook_id=webhook_id, event_type=event_type,
                attempts_cap=attempt, last_status=last_status,
                last_error=last_error,
            )
            return False

        if attempt < attempts_cap:
            await asyncio.sleep(RETRY_DELAYS[attempt - 1])

    # All attempts exhausted → DLQ + bump + maybe open breaker.
    _drop_to_dlq(
        webhook_id=webhook_id, event_type=event_type,
        body_bytes=body_bytes, last_error=_compose_err(last_status, last_error),
        attempts=attempts_cap,
        first_failed_at=first_failed_at or int(time.time()),
    )
    _bump_and_maybe_break(
        webhook_id=webhook_id, event_type=event_type,
        attempts_cap=attempts_cap, last_status=last_status,
        last_error=last_error,
    )
    return False


def _compose_err(status: Optional[int], error: Optional[str]) -> str:
    if status is not None:
        return f"http {status}" + (f": {error}" if error else "")
    return error or "unknown"


def _drop_to_dlq(
    *,
    webhook_id: int,
    event_type: str,
    body_bytes: bytes,
    last_error: str,
    attempts: int,
    first_failed_at: int,
) -> None:
    """Best-effort insert into webhook_dead_letter."""
    try:
        db.record_webhook_dead_letter(
            subscription_id=webhook_id,
            event_type=event_type,
            payload=body_bytes.decode(errors="replace"),
            last_error=last_error[:1000],
            attempts=attempts,
            first_failed_at=first_failed_at,
        )
    except Exception as exc:
        log.warning("DLQ insert failed wh=%s: %s", webhook_id, exc)


def _bump_and_maybe_break(
    *,
    webhook_id: int,
    event_type: str,
    attempts_cap: int,
    last_status: Optional[int],
    last_error: Optional[str],
) -> None:
    """Increment consecutive_failures and trip the circuit breaker if
    we've hit the threshold."""
    try:
        consecutive = db.bump_webhook_failure(webhook_id)
    except Exception:
        consecutive = 0
    log.warning(
        "webhook %s failed event=%s attempts=%d consecutive=%d last=%s err=%s",
        webhook_id, event_type, attempts_cap, consecutive,
        last_status, last_error,
    )
    if consecutive >= CIRCUIT_BREAKER_THRESHOLD:
        cooldown_until = int(time.time()) + CIRCUIT_BREAKER_COOLDOWN_S
        try:
            db.open_webhook_circuit(webhook_id, cooldown_until)
        except Exception as exc:
            log.warning("open_webhook_circuit failed wh=%s: %s", webhook_id, exc)
        try:
            _enqueue_disabled_email(webhook_id, consecutive)
        except Exception:
            pass


def _enqueue_disabled_email(webhook_id: int, consecutive: int) -> None:
    """Email the owner that we opened the circuit breaker.

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
        "cooldown_hours": CIRCUIT_BREAKER_COOLDOWN_S // 3600,
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


def _circuit_open(sub) -> bool:
    """Return True if the breaker is currently open on this subscription."""
    try:
        until = sub["disabled_until"]
    except (KeyError, IndexError):
        return False
    if until is None:
        return False
    return int(until) > int(time.time())


async def broadcast_event(event_type: str, payload: dict) -> int:
    """Fan *event_type* out to every active subscription listening for it.

    Returns the number of subscriptions dispatched to (not necessarily
    succeeded). Delivery is gathered with asyncio.gather so one slow
    subscriber never blocks another.

    Subscriptions with an open circuit breaker are skipped without
    counting toward the dispatched total.
    """
    if event_type not in EVENT_TYPES:
        log.debug("broadcast_event: unknown event_type=%s — ignoring", event_type)
        return 0
    try:
        subs = db.list_active_webhooks_for_event(event_type)
    except Exception as exc:
        log.warning("list_active_webhooks_for_event failed: %s", exc)
        return 0
    # Filter out subscriptions whose circuit breaker is open.
    subs = [s for s in subs if not _circuit_open(s)]
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
    """Hook us into the realtime hub so every internal broadcast also fans
    out to external webhook subscribers. No-op if the hub module isn't
    present or hasn't exposed register_after_broadcast yet — it's fine
    for a deploy to ship with only one half of this bridge.
    """
    try:
        from realtime.hub import hub as _singleton
    except Exception:
        log.info("webhooks: realtime.hub not available — external fan-out inactive")
        return
    if not hasattr(_singleton, "register_after_broadcast"):
        log.info("webhooks: hub lacks register_after_broadcast — skipping bridge")
        return
    try:
        _singleton.register_after_broadcast(hub_bridge)
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


# ── DLQ replay ──────────────────────────────────────────────────────────


async def replay_dead_letter(dlq_id: int) -> dict:
    """Re-deliver a DLQ entry. Marks the row as ``requeued_at`` regardless
    of outcome — the goal is to clear the admin backlog, and a successful
    replay shouldn't leave the entry visible alongside fresh failures.

    Re-uses the standard retry path so a flaky-but-recovering subscriber
    still gets the 2s/4s/8s ladder."""
    row = db.get_webhook_dead_letter(dlq_id)
    if not row:
        return {"ok": False, "error": "dlq row not found"}
    sub = db.get_webhook_subscription(row["subscription_id"])
    if not sub:
        return {"ok": False, "error": "subscription deleted"}
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return {"ok": False, "error": "payload not parseable as JSON"}

    ok = await _deliver_with_retries(
        webhook_id=sub["id"], url=sub["url"], secret=sub["secret"],
        event_type=row["event_type"], payload=payload,
    )
    try:
        db.mark_webhook_dead_letter_requeued(dlq_id)
    except Exception:
        pass
    return {"ok": ok}
