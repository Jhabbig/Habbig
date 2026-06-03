"""WebSocket subscriber for unusual_whales realtime feed.

Replaces the 2-minute poll in options_flow.py with sub-second push for
users on the unusual_whales WebSocket tier. The HTTP poller in
options_flow.py stays — both can run simultaneously (DB upserts dedupe
by alert_id / print_id), or the poller can be disabled by setting
OPTIONS_FLOW_INTERVAL_S to a very large number.

Reconnect logic: exponential backoff up to 60s. Persists every event,
then broadcasts via the existing events bus so SSE clients see new
options flow / dark-pool prints the moment they land.

Reference: https://unusualwhales.com/api  (WebSocket section)
Endpoint format is vendor-specific; we accept whatever payload shape
the WS sends and route to the appropriate options_flow normaliser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import db
import events
import options_flow

log = logging.getLogger("options_ws")

UW_WS_URL  = os.environ.get(
    "UNUSUAL_WHALES_WS_URL", "wss://api.unusualwhales.com/socket"
)
UW_API_KEY = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
CHANNELS   = [c.strip() for c in
              os.environ.get("UNUSUAL_WHALES_WS_CHANNELS",
                             "options-flow,darkpool").split(",")
              if c.strip()]

_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX     = 60.0


def is_configured() -> bool:
    return bool(UW_API_KEY and UW_WS_URL)


async def run_forever() -> None:
    """Background task — subscribes, parses, persists, broadcasts."""
    try:
        import websockets  # imported lazily; not a hard dep
    except ImportError:
        log.warning("websockets not installed — WS push disabled. "
                    "pip install websockets to enable.")
        return

    if not is_configured():
        log.info("unusual_whales WS not configured — skipping WS subscriber")
        return

    backoff = _BACKOFF_INITIAL
    headers = {"Authorization": f"Bearer {UW_API_KEY}"}

    log.info("unusual_whales WS starting: url=%s channels=%s", UW_WS_URL, CHANNELS)
    while True:
        try:
            async with websockets.connect(
                UW_WS_URL,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=20,
                close_timeout=5,
                max_size=2 * 1024 * 1024,
            ) as ws:
                # Subscribe to all configured channels.
                for ch in CHANNELS:
                    await ws.send(json.dumps({"action": "subscribe", "channel": ch}))
                backoff = _BACKOFF_INITIAL

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    await _handle(msg)
        except Exception as e:
            log.info("uw ws disconnected: %s; reconnecting in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)


async def _handle(msg: dict) -> None:
    """Dispatch a WS message to the right table.

    Vendor payloads vary; we sniff for fields. Most flow messages have
    `strike` / `option_type`; dark-pool messages don't.
    """
    if not isinstance(msg, dict):
        return
    channel = (msg.get("channel") or "").lower()
    data = msg.get("data") if isinstance(msg.get("data"), dict) else msg

    is_flow = (
        "flow" in channel or
        "strike" in data or "option_type" in data or
        "side" in data or "is_sweep" in data or "sweep" in data
    )
    is_dp = (
        "dark" in channel or "darkpool" in channel or
        ("size" in data and "price" in data and "strike" not in data)
    )

    if is_flow:
        row = options_flow._normalise_flow(data)
        if not row.get("alert_id") or not row.get("ticker"):
            return
        db.upsert_options_flow([row])
        events.broadcast("options_flow", row)
    elif is_dp:
        row = options_flow._normalise_dp(data)
        if not row.get("print_id") or not row.get("ticker"):
            return
        db.upsert_dark_pool([row])
        events.broadcast("dark_pool", row)
