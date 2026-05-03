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

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from . import audit, kalshi_auth, key_store

log = logging.getLogger(__name__)


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
        except Exception:
            err_body = ""
        return False, {}, f"HTTP {exc.code}: {err_body or exc.reason}"
    except urllib.error.URLError as exc:
        return False, {}, f"URLError: {exc.reason}"
    except Exception as exc:
        return False, {}, f"unexpected: {exc.__class__.__name__}: {exc}"


def _signed_call(user_id: str, method: str, path: str, body: dict | None = None) -> dict:
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
    # Drop the in-memory key reference as soon as we've signed.
    private_key = None  # noqa: F841

    ok, response, error = _request(signed)
    return {
        "ok": ok,
        "mode": stored.mode,
        "response": response if ok else None,
        "error": error,
    }


# --- Read endpoints --------------------------------------------------------

def get_balance(user_id: str) -> dict:
    result = _signed_call(user_id, "GET", "/trade-api/v2/portfolio/balance")
    audit.write_event(
        user_id, "balance.read",
        ok=result["ok"], response=result["response"],
        error=result["error"], mode=result.get("mode"),
    )
    return result


def get_positions(user_id: str) -> dict:
    result = _signed_call(user_id, "GET", "/trade-api/v2/portfolio/positions")
    audit.write_event(
        user_id, "positions.read",
        ok=result["ok"], response=result["response"],
        error=result["error"], mode=result.get("mode"),
    )
    return result


def list_orders(user_id: str) -> dict:
    result = _signed_call(user_id, "GET", "/trade-api/v2/portfolio/orders")
    audit.write_event(
        user_id, "orders.list",
        ok=result["ok"], response=result["response"],
        error=result["error"], mode=result.get("mode"),
    )
    return result


# --- Write endpoints (require explicit confirm) ----------------------------

def place_order(
    user_id: str,
    *,
    ticker: str,
    side: str,             # "yes" | "no"
    action: str,           # "buy" | "sell"
    count: int,
    price_cents: int,      # 1 to 99 — the Kalshi YES-side price the user is willing to pay
    time_in_force: str = "GTC",
    client_order_id: str | None = None,
) -> dict:
    """Place a limit order on Kalshi. The caller must have collected explicit
    user consent (a confirm-dialog click) before invoking this."""
    if side not in ("yes", "no"):
        return {"ok": False, "error": "side must be 'yes' or 'no'"}
    if action not in ("buy", "sell"):
        return {"ok": False, "error": "action must be 'buy' or 'sell'"}
    if not isinstance(count, int) or count < 1:
        return {"ok": False, "error": "count must be a positive integer"}
    if not isinstance(price_cents, int) or not (1 <= price_cents <= 99):
        return {"ok": False, "error": "price_cents must be an integer in [1, 99]"}

    body: dict[str, Any] = {
        "ticker": ticker,
        "type": "limit",
        "side": side,
        "action": action,
        "count": count,
        "time_in_force": time_in_force,
    }
    # Kalshi expects yes_price for yes-side orders, no_price for no-side.
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents
    if client_order_id:
        body["client_order_id"] = client_order_id

    result = _signed_call(user_id, "POST", "/trade-api/v2/portfolio/orders", body=body)
    audit.write_event(
        user_id, "order.place",
        ok=result["ok"],
        request=body,
        response=result["response"],
        error=result["error"],
        mode=result.get("mode"),
    )
    return result


def cancel_order(user_id: str, order_id: str) -> dict:
    if not order_id or "/" in order_id:
        return {"ok": False, "error": "invalid order_id"}
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    result = _signed_call(user_id, "DELETE", path)
    audit.write_event(
        user_id, "order.cancel",
        ok=result["ok"],
        request={"order_id": order_id},
        response=result["response"],
        error=result["error"],
        mode=result.get("mode"),
    )
    return result
