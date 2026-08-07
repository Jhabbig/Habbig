"""Authenticated Kalshi calls — balance, positions, place/cancel orders.

Every method here:

  1. Looks up the caller's stored credentials via :mod:`key_store` (decrypts
     just the credentials needed for this single call).
  2. Builds the signed request via :mod:`kalshi_auth`.
  3. Fires it (urllib, stdlib only).
  4. Writes an :mod:`audit` event for the action — both success and failure.

The entry-point functions take a ``user_id`` and return a Python dict with
``ok``, the parsed JSON body when present, and on failure a sanitized
``error`` message. We never raise — callers in ``server.py`` translate the
result dict into HTTP status codes.

Order placement specifically:
  * v0.8 supports the canonical Kalshi ``orders`` payload: ``ticker``,
    ``side`` ("yes"/"no"), ``action`` ("buy"/"sell"), ``type`` ("limit"),
    ``count``, ``yes_price`` (for "yes" side) **or** ``no_price``
    (for "no" side), and ``time_in_force`` defaulting to "GTC".
  * Market orders, IOC fills, and complex order types are out of scope.
    A limit order at the user's chosen price covers the FOMC use case
    (these markets tighten only in the days before settlement).
  * The user must explicitly send a ``confirm=true`` flag in the request —
    the server.py route enforces this. The order_manager assumes the
    caller already collected that consent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from . import audit, kalshi_auth, key_store

log = logging.getLogger(__name__)


# ── Concurrency + idempotency state (C2, C3, H10) ────────────────────────────
#
# `_user_locks`         — per-user asyncio.Lock so two concurrent place/cancel
#                         requests for the same user serialize. Trading is not
#                         a hot path; a per-user lock is fine.
# `_seen_client_oids`   — per-user dict of client_order_id → ts_seconds.
#                         An identical client_order_id from the same user
#                         within DEDUPE_WINDOW_S is rejected with
#                         {"duplicate": True}. Old entries are pruned lazily.
# `_last_order_ts`      — per-user timestamp of the most recent order.place
#                         call; used by the 1-req/sec rate limiter.
# `_balance_cache`      — per-user (ts, payload) tuple to avoid re-fetching
#                         balance on every preflight check. 5s TTL.

_user_locks: dict[str, asyncio.Lock] = {}
_user_locks_guard = threading.Lock()

DEDUPE_WINDOW_S = 60
_seen_client_oids: dict[str, dict[str, float]] = {}
_dedupe_guard = threading.Lock()

RATE_LIMIT_S = 1.0
_last_order_ts: dict[str, float] = {}
_rate_guard = threading.Lock()

_BALANCE_TTL_S = 5.0
_balance_cache: dict[str, tuple[float, dict]] = {}
_balance_guard = threading.Lock()


def _get_user_lock(user_id: str) -> asyncio.Lock:
    with _user_locks_guard:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[user_id] = lock
        return lock


def _check_and_record_dedupe(user_id: str, client_oid: str) -> bool:
    """Return True if (user_id, client_oid) has been seen in the last
    DEDUPE_WINDOW_S seconds. Otherwise record it and return False."""
    now = time.time()
    with _dedupe_guard:
        per_user = _seen_client_oids.setdefault(user_id, {})
        # Lazy prune
        for stale_oid in [k for k, ts in per_user.items() if now - ts > DEDUPE_WINDOW_S]:
            per_user.pop(stale_oid, None)
        if client_oid in per_user:
            return True
        per_user[client_oid] = now
        return False


def _check_rate_limit(user_id: str) -> float:
    """Return seconds-since-last-order. Caller compares against RATE_LIMIT_S."""
    now = time.time()
    with _rate_guard:
        last = _last_order_ts.get(user_id, 0.0)
        return now - last


def _record_order_ts(user_id: str) -> None:
    with _rate_guard:
        _last_order_ts[user_id] = time.time()


def _request(signed: kalshi_auth.SignedRequest, timeout: float = 15.0) -> tuple[bool, dict, str | None]:
    """Fire a signed request. Returns ``(ok, body, error)``.

    ``ok`` is True for HTTP 2xx; ``body`` is the parsed JSON or {} on parse
    failure; ``error`` is None on success and a short string on failure.
    """
    req = urllib.request.Request(
        signed.full_url,
        data=signed.body,
        method=signed.method,
        headers=signed.headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return True, (json.loads(raw) if raw else {}), None
            except json.JSONDecodeError:
                return True, {"_raw": raw[:500]}, None
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
        except OSError:
            err_body = ""
        return False, {}, f"HTTP {exc.code}: {err_body or exc.reason}"
    except urllib.error.URLError as exc:
        return False, {}, f"URLError: {exc.reason}"
    # M6: only catch concrete network errors. Programmer errors (TypeError,
    # AttributeError) should propagate so they show up in logs as bugs, not
    # as fake "network failure" strings the user sees in the UI.
    except (socket.timeout, ssl.SSLError, TimeoutError, ConnectionError) as exc:
        return False, {}, f"network: {exc.__class__.__name__}: {exc}"


def _signed_call(
    user_id: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """Build + execute a signed call on behalf of ``user_id``. Returns a dict
    suitable for direct JSON-serialization back to the dashboard frontend."""
    stored = key_store.get_key(user_id)
    if stored is None:
        return {"ok": False, "error": "no_kalshi_credentials_configured"}

    body_bytes = None
    if body is not None:
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")

    private_key = kalshi_auth.load_private_key(stored.private_key_pem)
    signed = kalshi_auth.build_signed_request(
        method=method,
        path=path,
        api_key_id=stored.api_key_id,
        private_key=private_key,
        mode=stored.mode,
        body=body_bytes,
    )
    if extra_headers:
        signed.headers.update(extra_headers)
    # Drop the in-memory key reference as soon as we've signed.
    private_key = None  # noqa: F841

    ok, response, error = _request(signed)
    return {
        "ok": ok,
        "mode": stored.mode,
        "response": response if ok else None,
        "error": error,
    }


def _safe_audit(user_id: str, action: str, **fields) -> bool:
    """Wrapper around :mod:`audit.write_event` that returns False on failure
    rather than raising. Caller can then surface an audit_warning."""
    try:
        audit.write_event(user_id, action, **fields)
        return True
    except OSError as exc:
        log.error("audit.write failed for %s/%s: %s", user_id, action, exc)
        return False


# --- Read endpoints --------------------------------------------------------


def get_balance(user_id: str) -> dict:
    result = _signed_call(user_id, "GET", "/trade-api/v2/portfolio/balance")
    _safe_audit(
        user_id,
        "balance.read",
        ok=result["ok"],
        response=result["response"],
        error=result["error"],
        mode=result.get("mode"),
    )
    # Refresh the per-user balance cache opportunistically — any successful
    # GET /balance response is fresh enough to be reused for preflights
    # against insufficient-funds for the next few seconds.
    if result.get("ok") and result.get("response"):
        with _balance_guard:
            _balance_cache[user_id] = (time.time(), result["response"])
    return result


def _cached_balance_cents(user_id: str) -> int | None:
    """Return cached balance in cents, or None if not cached."""
    with _balance_guard:
        entry = _balance_cache.get(user_id)
    if not entry:
        return None
    ts, payload = entry
    if time.time() - ts > _BALANCE_TTL_S:
        return None
    # Kalshi returns ``balance`` in cents on /portfolio/balance.
    bal = payload.get("balance")
    if isinstance(bal, (int, float)):
        return int(bal)
    return None


def get_positions(user_id: str) -> dict:
    result = _signed_call(user_id, "GET", "/trade-api/v2/portfolio/positions")
    _safe_audit(
        user_id,
        "positions.read",
        ok=result["ok"],
        response=result["response"],
        error=result["error"],
        mode=result.get("mode"),
    )
    return result


def list_orders(user_id: str) -> dict:
    result = _signed_call(user_id, "GET", "/trade-api/v2/portfolio/orders")
    _safe_audit(
        user_id,
        "orders.list",
        ok=result["ok"],
        response=result["response"],
        error=result["error"],
        mode=result.get("mode"),
    )
    return result


# --- Write endpoints (require explicit confirm) ----------------------------


def place_order(
    user_id: str,
    *,
    ticker: str,
    side: str,  # "yes" | "no"
    action: str,  # "buy" | "sell"
    count: int,
    price_cents: int,  # 1 to 99 — the Kalshi YES-side price the user is willing to pay
    time_in_force: str = "GTC",
    client_order_id: str | None = None,
) -> dict:
    """Place a limit order on Kalshi. The caller must have collected explicit
    user consent (a confirm-dialog click) before invoking this.

    C2 / H10 contract:
      * ``client_order_id`` is *server-mandatory*. If the caller passes None
        we generate one (``cb-<uuid8>``). The same id within DEDUPE_WINDOW_S
        for the same user returns ``{"duplicate": True}`` rather than firing
        a second order.
      * The id is also forwarded as the Kalshi ``Idempotency-Key`` header so
        Kalshi itself dedupes if our retry crosses a network blip.
    """
    if side not in ("yes", "no"):
        return {"ok": False, "error": "side must be 'yes' or 'no'"}
    if action not in ("buy", "sell"):
        return {"ok": False, "error": "action must be 'buy' or 'sell'"}
    if not isinstance(count, int) or count < 1:
        return {"ok": False, "error": "count must be a positive integer"}
    if not isinstance(price_cents, int) or not (1 <= price_cents <= 99):
        return {"ok": False, "error": "price_cents must be an integer in [1, 99]"}

    # H10 rate limit
    elapsed = _check_rate_limit(user_id)
    if elapsed < RATE_LIMIT_S:
        return {
            "ok": False,
            "rate_limited": True,
            "error": f"rate limited: {RATE_LIMIT_S}s minimum between orders (elapsed {elapsed:.2f}s)",
        }

    # C2: server-issued client_order_id when caller didn't supply one.
    if not client_order_id:
        client_order_id = f"cb-{uuid.uuid4().hex[:16]}"
    if _check_and_record_dedupe(user_id, client_order_id):
        return {
            "ok": False,
            "duplicate": True,
            "client_order_id": client_order_id,
            "error": "duplicate order: same client_order_id seen in last 60s",
        }

    # H7: balance preflight. We use the cached balance if available; if not,
    # fetch fresh. This keeps order latency low and avoids hammering Kalshi
    # on every confirm-click.
    cost_cents = count * price_cents
    bal_cents = _cached_balance_cents(user_id)
    if bal_cents is None:
        bal_resp = get_balance(user_id)
        if bal_resp.get("ok"):
            bal_cents = _cached_balance_cents(user_id)
    if bal_cents is not None and cost_cents > bal_cents:
        return {
            "ok": False,
            "error": f"insufficient balance: order needs {cost_cents}¢, account has {bal_cents}¢",
        }

    body: dict[str, Any] = {
        "ticker": ticker,
        "type": "limit",
        "side": side,
        "action": action,
        "count": count,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
    }
    # Kalshi expects yes_price for yes-side orders, no_price for no-side.
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents

    # H10: also send Idempotency-Key so Kalshi-side dedupes a network retry.
    extra_headers = {"Idempotency-Key": client_order_id}
    result = _signed_call(
        user_id,
        "POST",
        "/trade-api/v2/portfolio/orders",
        body=body,
        extra_headers=extra_headers,
    )
    _record_order_ts(user_id)

    # Stamp the client_order_id back into the result so the UI can show it
    # next to the resting order in the orders panel.
    result["client_order_id"] = client_order_id

    audit_ok = _safe_audit(
        user_id,
        "order.place",
        ok=result["ok"],
        request=body,
        response=result["response"],
        error=result["error"],
        mode=result.get("mode"),
    )
    if not audit_ok:
        # H8: surface so the user can reconcile manually if the disk's full.
        result["audit_warning"] = "audit log write failed; please reconcile this order against Kalshi manually"
    return result


def cancel_order(user_id: str, order_id: str) -> dict:
    if not order_id or "/" in order_id:
        return {"ok": False, "error": "invalid order_id"}
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    result = _signed_call(user_id, "DELETE", path)
    audit_ok = _safe_audit(
        user_id,
        "order.cancel",
        ok=result["ok"],
        request={"order_id": order_id},
        response=result["response"],
        error=result["error"],
        mode=result.get("mode"),
    )
    if not audit_ok:
        result["audit_warning"] = "audit log write failed; cancellation completed but not logged locally"
    return result


def verify_credentials_for_mode(user_id: str, mode: str) -> dict:
    """H13: call /portfolio/balance against the host implied by ``mode`` to
    verify the stored credentials match it. Used by the mode-switch endpoint
    so a paper-key+prod-mode mismatch is caught at toggle time, not at order
    placement time."""
    stored = key_store.get_key(user_id)
    if stored is None:
        return {"ok": False, "error": "no_kalshi_credentials_configured"}
    private_key = kalshi_auth.load_private_key(stored.private_key_pem)
    signed = kalshi_auth.build_signed_request(
        method="GET",
        path="/trade-api/v2/portfolio/balance",
        api_key_id=stored.api_key_id,
        private_key=private_key,
        mode=mode,  # NB: caller-supplied mode, not stored.mode
    )
    private_key = None  # noqa: F841
    ok, _resp, error = _request(signed, timeout=10.0)
    return {"ok": ok, "error": error, "mode": mode}


# --- Async wrappers (C3 + C4) ----------------------------------------------
#
# server.py's route handlers are async. We use these wrappers to:
#   1. Take the per-user lock so simultaneous orders/cancels from the same
#      user serialize (no race on the dedupe map or Kalshi).
#   2. Run the underlying sync function via `asyncio.to_thread` so the
#      blocking urllib call doesn't freeze the event loop.


async def place_order_async(user_id: str, **kwargs) -> dict:
    lock = _get_user_lock(user_id)
    async with lock:
        return await asyncio.to_thread(place_order, user_id, **kwargs)


async def cancel_order_async(user_id: str, order_id: str) -> dict:
    lock = _get_user_lock(user_id)
    async with lock:
        return await asyncio.to_thread(cancel_order, user_id, order_id)
