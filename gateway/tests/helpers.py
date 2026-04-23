"""Test-suite helpers — factories, auth shortcuts, and Stripe-signed
webhook builders.

Import from `tests.helpers` in any test that needs something heavier
than a bare fixture call. Fixtures in ``conftest.py`` stay thin so
parameterisation is easy; these functions build the richer payloads.

Every factory accepts ``**overrides`` so callers can tweak a single
field without restating the full row::

    pred = make_prediction(source_handle="@jake", category="finance")
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Optional

import db


# ── ID plumbing ──────────────────────────────────────────────────────────

_counter = 0


def _seq(prefix: str = "") -> str:
    """Monotonic suffix for unique identifiers across a single run."""
    global _counter
    _counter += 1
    return f"{prefix}{_counter}_{int(time.time())}"


# ── Factories ────────────────────────────────────────────────────────────


def make_source(
    *,
    handle: Optional[str] = None,
    credibility: float = 0.6,
    total_predictions: int = 5,
    correct_predictions: int = 3,
    categories_active: int = 1,
    **overrides: Any,
) -> dict:
    """Insert a row into source_credibility and return the dict form.

    The table is UNIQUE on source_handle — pass a fixed handle to upsert,
    or omit to auto-generate a unique one.
    """
    handle = handle or _seq("src")
    now = int(time.time())
    row = {
        "source_handle": handle,
        "global_credibility": credibility,
        "total_predictions": total_predictions,
        "correct_predictions": correct_predictions,
        "categories_active": categories_active,
        "last_computed_at": now,
    }
    row.update(overrides)
    with db.conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO source_credibility "
            "(source_handle, global_credibility, total_predictions, "
            " correct_predictions, categories_active, last_computed_at) "
            "VALUES (?,?,?,?,?,?)",
            (row["source_handle"], row["global_credibility"],
             row["total_predictions"], row["correct_predictions"],
             row["categories_active"], row["last_computed_at"]),
        )
    return row


def make_prediction(
    *,
    source_handle: Optional[str] = None,
    market_id: str = "poly:test-market",
    category: str = "other",
    direction: str = "yes",
    predicted_probability: float = 0.7,
    content: str = "Test prediction",
    extracted_at: Optional[int] = None,
    resolved: int = 0,
    resolved_correct: Optional[int] = None,
    **overrides: Any,
) -> dict:
    """Insert a row into predictions and return (id, *row)."""
    if source_handle is None:
        source_handle = make_source()["source_handle"]
    row = {
        "source_handle": source_handle,
        "market_id": market_id,
        "category": category,
        "direction": direction,
        "predicted_probability": predicted_probability,
        "content": content,
        "extracted_at": extracted_at or int(time.time()),
        "resolved": resolved,
        "resolved_correct": resolved_correct,
    }
    row.update(overrides)
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO predictions "
            "(source_handle, market_id, category, direction, "
            " predicted_probability, content, extracted_at, resolved, "
            " resolved_correct) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (row["source_handle"], row["market_id"], row["category"],
             row["direction"], row["predicted_probability"], row["content"],
             row["extracted_at"], row["resolved"], row["resolved_correct"]),
        )
        row["id"] = int(cur.lastrowid)
    return row


def make_market(
    *,
    market_id: str = "poly:test-market",
    title: str = "Will it test?",
    category: str = "other",
    yes_price: float = 0.5,
    volume_usd: float = 100_000,
) -> dict:
    """Build a fake UnifiedMarket dict. The gateway doesn't keep a
    persistent markets table — the market cache is in-memory — so this
    helper just returns a dict the caller can slot into a mock."""
    return {
        "id": market_id,
        "source": "polymarket" if market_id.startswith("poly") else "kalshi",
        "title": title,
        "category": category,
        "yes_price": yes_price,
        "no_price": 1 - yes_price,
        "volume_usd": volume_usd,
        "liquidity_usd": volume_usd / 10,
        "status": "active",
        "outcome": None,
        "url": f"https://polymarket.com/{market_id.split(':', 1)[-1]}",
        "close_time": None,
    }


# ── HTTP helpers ─────────────────────────────────────────────────────────


def csrf_headers(session_token: Optional[str] = None) -> dict:
    """Return the cookie + header pair needed for any POST/PATCH/DELETE.

    If ``session_token`` is given the returned dict also sets the
    session cookie so the request is authenticated as that user."""
    cookies = []
    if session_token:
        cookies.append(f"pm_gateway_session={session_token}")
    cookies.append("_csrf=t")
    return {
        "Cookie": "; ".join(cookies),
        "x-csrf-token": "t",
    }


# ── Stripe webhook signing ───────────────────────────────────────────────


def signed_stripe_event(
    event_type: str,
    data: dict,
    *,
    secret: str = "whsec_test",
    timestamp: Optional[int] = None,
) -> tuple[bytes, dict]:
    """Build a (body, headers) tuple for a Stripe-webhook TestClient call.

    The Stripe-Signature header matches Stripe's scheme:
        t=<ts>,v1=<hmac_sha256(ts + "." + payload, secret)>
    """
    ts = int(timestamp or time.time())
    payload = {
        "id": f"evt_{_seq('test')}",
        "object": "event",
        "type": event_type,
        "created": ts,
        "data": {"object": data},
        "api_version": "2024-04-10",
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signed_payload = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    headers = {
        "stripe-signature": f"t={ts},v1={sig}",
        "content-type": "application/json",
    }
    return body, headers


# ── DB cleanup ───────────────────────────────────────────────────────────


def clear_tables(*tables: str) -> None:
    """Rip every row out of the named tables. Used by tests that need a
    hermetic starting state even under the shared-conn model."""
    with db.conn() as c:
        for t in tables:
            try:
                c.execute(f"DELETE FROM {t}")
            except Exception:
                pass
