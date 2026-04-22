"""Fire-and-forget wrappers around ``hub.broadcast``.

Write sites shouldn't care about whether they're inside an event loop or
a sync context — they just call ``emit_new_prediction(...)`` and continue.
This module does the ``asyncio.get_running_loop / ensure_future`` dance
and swallows every exception so a realtime hiccup never breaks a DB write.

Keep helper signatures thin — add new ones for new event types rather
than bloat existing ones with optional kwargs. Channel + payload shape
both belong to the PUBLIC API seen by every JS client, so treat these
function bodies as a schema.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .hub import hub


log = logging.getLogger("gateway.realtime.broadcast")


def _schedule(coro) -> None:
    """Run ``coro`` on the current event loop if there is one, else run it
    to completion in a fresh loop. Never raises into the caller."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop is None:
            asyncio.run(coro)
        else:
            loop.create_task(coro)
    except Exception as exc:  # pragma: no cover
        log.debug("broadcast schedule failed: %s", exc)


def _emit(channel: str, payload: dict) -> None:
    _schedule(hub.broadcast(channel, payload))


# ── Event helpers ──────────────────────────────────────────────────────────


def emit_new_prediction(
    *,
    source_handle: str,
    market_slug: str | None,
    category: str,
    direction: str | None,
    predicted_probability: Any,
    content: str,
) -> None:
    """Fan new predictions to both the per-market channel and the global feed.

    Truncate ``content`` to 280 chars so a pathological claim can't fatten
    the envelope. Full content is always available at
    ``/api/markets/{id}/probability`` if a client wants it.
    """
    prediction = {
        "source_handle": source_handle,
        "market_slug": market_slug,
        "category": category,
        "direction": direction,
        "predicted_probability": predicted_probability,
        "content": (content or "")[:280],
    }
    # Per-market — only if we know the market it's about.
    if market_slug:
        _emit(f"market:{market_slug}", {
            "type": "new_prediction",
            "prediction": prediction,
        })
    # Global feed — every subscriber cares.
    _emit("feed:global", {
        "type": "new_prediction",
        "prediction": prediction,
    })


def emit_price_tick(
    *,
    market_slug: str,
    yes_price: float,
    no_price: float | None = None,
    volume_24h: float | None = None,
) -> None:
    """Emit a market price / probability update."""
    _emit(f"market:{market_slug}", {
        "type": "price_tick",
        "yes_price": yes_price,
        "no_price": no_price if no_price is not None else (1.0 - yes_price if yes_price is not None else None),
        "volume_24h": volume_24h,
    })


def emit_notification(*, user_id: int, notification: dict) -> None:
    """Push a notification to the specific user's channel."""
    _emit(f"user:{int(user_id)}", {
        "type": "notification",
        "notification": notification,
    })


def emit_credibility_update(*, source_handle: str, global_credibility: float, market_slug: str | None = None) -> None:
    """Tell every subscriber listening on this market that the credibility
    engine recomputed. If the market scope is unknown, no-op on the market
    channel but still surface the event on the feed so dashboards that
    display source-leaderboards refresh."""
    payload = {
        "type": "credibility_update",
        "source_handle": source_handle,
        "global_credibility": global_credibility,
    }
    if market_slug:
        _emit(f"market:{market_slug}", payload)
    _emit("feed:global", payload)


def emit_capture_attempt(*, user_id: int | None, kind: str, context: dict | None = None) -> None:
    """Notify the admin:security channel of a capture / forensic alert."""
    _emit("admin:security", {
        "type": "capture_attempt",
        "user_id": user_id,
        "kind": kind,
        "context": context or {},
    })
