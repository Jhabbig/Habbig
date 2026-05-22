from __future__ import annotations
"""WebSocket live feed.

Single endpoint /ws/feed. Clients connect, receive a hello with the latest
50 filings, then receive a JSON message for every new filing as it lands.

Implementation: a single async fanout task polls the DB every N seconds for
filings with id > the high-water mark, broadcasts the new ones to all
connected clients, then advances the mark. This avoids cross-module event
plumbing — ingesters write to SQLite as normal, the fanout discovers new
rows on its own poll cadence.

That polling cost is trivial (a single COUNT-equivalent query on indexed
ids) and means the feed survives ingester crashes without losing messages.

Auth: handled by the gateway. By the time the WS upgrade reaches us, the
gateway has already verified the user; we read x-gateway-user-id from the
upgrade request headers as the SSO identity. If the gateway secret is set,
we require it.
"""

import asyncio
import hmac
import json
import logging
import os
from typing import Set

from fastapi import WebSocket, WebSocketDisconnect

from database import get_conn

logger = logging.getLogger(__name__)


_clients: Set[WebSocket] = set()
_lock = asyncio.Lock()
_high_water = {"13f": 0, "form4": 0, "13d": 0}
_POLL_S = float(os.getenv("WS_POLL_S", "5.0"))


def _auth_ok(ws: WebSocket) -> bool:
    secret = os.environ.get("GATEWAY_SSO_SECRET")
    provided = ws.headers.get("x-gateway-secret", "")
    if not secret:
        # No secret configured — dev mode, accept everything.
        return True
    return hmac.compare_digest(provided, secret)


async def _send_hello(ws: WebSocket) -> None:
    """Send recent filings on connect so a fresh tab has something to show."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT 'edgar_13f' AS source, accession, form_type AS kind,
                      filed_date, total_value_usd AS value_usd,
                      NULL AS target_ticker, NULL AS target_name
                 FROM filings_13f
                 ORDER BY id DESC LIMIT 25"""
        ).fetchall()
    await ws.send_text(json.dumps({
        "type": "hello",
        "recent": [dict(r) for r in rows],
    }))


async def _broadcast(message: dict) -> None:
    if not _clients:
        return
    payload = json.dumps(message)
    dead: list[WebSocket] = []
    for ws in list(_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    if dead:
        async with _lock:
            for ws in dead:
                _clients.discard(ws)


async def _init_high_water() -> None:
    with get_conn() as conn:
        for source_key, table in [
            ("13f", "filings_13f"),
            ("form4", "insider_txns"),
            ("13d", "activist_filings"),
        ]:
            row = conn.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}").fetchone()
            _high_water[source_key] = int(row["m"])


async def fanout_loop() -> None:
    """Poll for new rows and broadcast them. Runs forever."""
    await _init_high_water()
    while True:
        try:
            await _poll_and_broadcast()
        except Exception:
            logger.exception("ws fanout: poll failed")
        await asyncio.sleep(_POLL_S)


async def _poll_and_broadcast() -> None:
    new_msgs: list[dict] = []
    with get_conn() as conn:
        # 13F
        rows = conn.execute(
            """SELECT f.id, f.accession, f.form_type, f.filed_date,
                      f.total_value_usd, e.parent_name AS filer
                 FROM filings_13f f
                 JOIN cik_map c ON c.cik=f.cik
                 JOIN entities e ON e.id=c.entity_id
                WHERE f.id > ?
                ORDER BY f.id ASC""",
            (_high_water["13f"],),
        ).fetchall()
        for r in rows:
            new_msgs.append({"type": "filing", "source": "edgar_13f", "data": dict(r)})
            _high_water["13f"] = max(_high_water["13f"], int(r["id"]))

        # Form 4
        rows = conn.execute(
            """SELECT id, accession, issuer_ticker, issuer_name,
                      insider_name, insider_role, txn_date, txn_code,
                      shares, price, value_usd
                 FROM insider_txns
                WHERE id > ?
                ORDER BY id ASC""",
            (_high_water["form4"],),
        ).fetchall()
        for r in rows:
            new_msgs.append({"type": "filing", "source": "edgar_form4", "data": dict(r)})
            _high_water["form4"] = max(_high_water["form4"], int(r["id"]))

        # 13D
        rows = conn.execute(
            """SELECT a.id, a.accession, a.schedule, a.filed_date,
                      a.target_ticker, a.target_name, a.ownership_pct,
                      a.intent_class, a.intent_score,
                      e.parent_name AS filer
                 FROM activist_filings a
                 LEFT JOIN entities e ON e.id=a.filer_entity_id
                WHERE a.id > ?
                ORDER BY a.id ASC""",
            (_high_water["13d"],),
        ).fetchall()
        for r in rows:
            new_msgs.append({"type": "filing", "source": "edgar_13d", "data": dict(r)})
            _high_water["13d"] = max(_high_water["13d"], int(r["id"]))

    for m in new_msgs:
        await _broadcast(m)


async def handle(ws: WebSocket) -> None:
    """Per-connection handler."""
    if not _auth_ok(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    async with _lock:
        _clients.add(ws)
    try:
        await _send_hello(ws)
        # Keep the connection open; we don't expect any client messages, but
        # we need to await receive() to detect disconnect.
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                # idle keepalive ping
                await ws.send_text(json.dumps({"type": "ping"}))
            except WebSocketDisconnect:
                return
    finally:
        async with _lock:
            _clients.discard(ws)
