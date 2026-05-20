"""WebSocket broadcast hub for sub-second price updates.

Maintains a set of connected clients, and a single async background loop
that polls a small set of hot signals (top-20 spot prices, F&G, BTC fees,
funding spread leaders) every N seconds and broadcasts a compact JSON
diff to every connected client.

Clients subscribe on `/ws/prices` and receive messages like:
  {"type": "tick", "ts": <epoch>, "prices": [{base, last, dpct}, ...],
   "fng": <val>, "btc_fee": <sat/vB>, "btc_height": <num>}

Designed so a single dashboard process can support thousands of concurrent
viewers without thundering-herd polling — clients no longer poll the REST
endpoints once the WS is connected.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from fastapi import WebSocket

from . import binance, coingecko, fear_greed, mempool_btc

log = logging.getLogger("ct.ws")

_CLIENTS: set[WebSocket] = set()
_LOCK = asyncio.Lock()
_TASK: Optional[asyncio.Task] = None


async def register(ws: WebSocket) -> None:
    await ws.accept()
    async with _LOCK:
        _CLIENTS.add(ws)
    log.info("WS client registered (now %d)", len(_CLIENTS))


async def unregister(ws: WebSocket) -> None:
    async with _LOCK:
        _CLIENTS.discard(ws)
    log.info("WS client unregistered (now %d)", len(_CLIENTS))


def client_count() -> int:
    return len(_CLIENTS)


async def _broadcast(msg: dict) -> None:
    async with _LOCK:
        dead: list[WebSocket] = []
        for ws in _CLIENTS:
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            _CLIENTS.discard(ws)


def _build_tick() -> dict:
    """Compose a compact tick from the cheapest signals we have on hand.

    Reads only cached/in-process state from the various ingestion modules.
    Every ingestion module already caches with TTL so this is fast - the
    background loop just snapshots and ships a delta-friendly payload.
    """
    out = {"type": "tick", "ts": time.time()}

    # Top-20 spot prices from CoinGecko cache (only if recently populated)
    try:
        univ = coingecko.universe(50)
        if univ and not univ.get("error"):
            out["prices"] = [{
                "id": c.get("id"),
                "sym": c.get("symbol"),
                "px": c.get("current_price"),
                "dpct": c.get("change_24h"),
            } for c in (univ.get("coins") or [])[:20]]
    except Exception:  # noqa: BLE001
        pass

    # Fear & Greed
    try:
        fng = fear_greed.index(1)
        if fng and not fng.get("error"):
            latest = fng.get("latest") or {}
            out["fng"] = latest.get("value")
    except Exception:  # noqa: BLE001
        pass

    # BTC mempool fees + height
    try:
        net = mempool_btc.network_status()
        if net and not net.get("error"):
            out["btc_fee"] = (net.get("fees_sat_per_vb") or {}).get("fastest")
            out["btc_height"] = net.get("block_height")
            mem = net.get("mempool") or {}
            out["btc_mempool"] = mem.get("count")
    except Exception:  # noqa: BLE001
        pass

    # Top BTC perp funding rate
    try:
        prem = binance.futures_premium_index()
        if prem and not prem.get("error"):
            for r in prem.get("rows") or []:
                if r.get("symbol") == "BTCUSDT":
                    out["btc_funding"] = r.get("funding_rate")
                    out["btc_mark"] = r.get("mark_price")
                    break
    except Exception:  # noqa: BLE001
        pass

    return out


async def broadcast_loop(interval_s: float = 5.0) -> None:
    """Single async task that polls every interval and broadcasts to all
    connected clients. Skips broadcast if there are no clients."""
    while True:
        try:
            if _CLIENTS:
                tick = _build_tick()
                await _broadcast(tick)
        except Exception as e:  # noqa: BLE001
            log.warning("broadcast tick error: %s", e)
        await asyncio.sleep(interval_s)


def start() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    loop = asyncio.get_event_loop()
    _TASK = loop.create_task(broadcast_loop())
    log.info("WS broadcast loop started")
