from __future__ import annotations
"""Polymarket WebSocket consumer.

Subscribes to live market updates and writes price deltas straight to the
``midterm_price_history`` table so the chart on the race detail page reflects
sub-minute moves between the 5-minute polling refresh cycles.

Wiring:
  - Enabled by setting POLYMARKET_WS_ENABLED=1 in the env.
  - Subscribes to the token_ids of every active polymarket market in the DB
    at startup, and re-subscribes when new tokens appear (after each refresh).

This is a best-effort transport; if the WS disconnects, the consumer
reconnects with exponential backoff. If it can't connect at all, the polling
loop still keeps data fresh on the 5-minute cadence.
"""

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketWebSocket:
    """Long-lived consumer for Polymarket's market WS channel."""

    def __init__(self, db, session: Optional[aiohttp.ClientSession] = None):
        self._db = db
        self._session = session
        self._owns_session = session is None
        self._token_ids: set[str] = set()
        self._stop = asyncio.Event()
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def stop(self) -> None:
        self._stop.set()

    def refresh_token_ids(self) -> None:
        """Reload subscribed token IDs from the active Polymarket markets in DB.

        We don't currently push a re-subscribe to the WS — that needs a server
        round-trip that Polymarket may not support. Instead the next reconnect
        cycle picks up the new set.
        """
        try:
            markets = self._db.get_markets(source="polymarket")
        except Exception as e:
            logger.warning(f"PolymarketWS: failed to load markets: {e}")
            return
        tokens: set[str] = set()
        for m in markets:
            for o in (m.get("outcomes") or []):
                tid = o.get("token_id")
                if tid:
                    tokens.add(str(tid))
        if tokens != self._token_ids:
            self._token_ids = tokens
            logger.info(f"PolymarketWS: tracking {len(tokens)} token IDs")

    async def run(self) -> None:
        """Connect, subscribe, process messages, reconnect on failure.

        Backoff: 2s → 60s with jitter. Stops cleanly when ``stop()`` is called.
        """
        attempt = 0
        while not self._stop.is_set():
            self.refresh_token_ids()
            if not self._token_ids:
                # Nothing to subscribe to yet — wait for the polling loop to seed.
                await asyncio.sleep(30)
                continue

            try:
                session = await self._get_session()
                async with session.ws_connect(POLYMARKET_WS_URL, heartbeat=30) as ws:
                    self._ws = ws
                    attempt = 0  # reset backoff on successful connect
                    sub_msg = {
                        "type": "market",
                        "assets_ids": list(self._token_ids),
                    }
                    await ws.send_json(sub_msg)
                    logger.info(f"PolymarketWS: subscribed to {len(self._token_ids)} assets")
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"PolymarketWS: ws closed/error: {msg.type}")
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"PolymarketWS: connection error: {e}")
            except Exception as e:
                logger.error(f"PolymarketWS: unexpected error: {e}", exc_info=True)

            if self._stop.is_set():
                break
            attempt += 1
            backoff = min(2 ** attempt, 60) + random.random()
            logger.info(f"PolymarketWS: reconnecting in {backoff:.1f}s")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass  # backoff elapsed normally

    def _handle_message(self, raw: str) -> None:
        """Decode and persist a single WS message.

        The Polymarket payload format is undocumented in detail; this handler
        treats anything with ``asset_id`` and ``price`` as a tradeable update.
        Other event types (book, ticker) are ignored — the polling loop will
        catch them on the next refresh.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        events = data if isinstance(data, list) else [data]
        ts = datetime.now(timezone.utc).isoformat()
        for ev in events:
            if not isinstance(ev, dict):
                continue
            asset_id = ev.get("asset_id") or ev.get("token_id")
            price = ev.get("price")
            if asset_id is None or price is None:
                continue
            try:
                price_f = float(price)
            except (TypeError, ValueError):
                continue
            # Best-effort write — log+swallow any DB error so the WS reader
            # keeps draining.
            try:
                # We store under the token_id rather than market_id because
                # the WS doesn't carry the internal market_id; consumers of
                # the price_history table can join by source+token_id.
                self._db.record_price_snapshot(
                    market_id=0, source="polymarket-ws",
                    prices={"asset_id": asset_id, "price": price_f, "ts": ts},
                    volume=None,
                )
            except Exception as e:
                logger.debug(f"PolymarketWS: db write failed: {e}")


def ws_enabled() -> bool:
    return os.getenv("POLYMARKET_WS_ENABLED", "").strip() not in ("", "0", "false", "False")
