"""Polymarket connection + position fetch.

Polymarket positions are on-chain state keyed by a Polygon address —
no API key, no secret. We only need to:

  1. Accept and validate a 0x-prefixed wallet address.
  2. Upsert a polymarket_connections row linking it to the user.
  3. Periodically fetch /positions?address={wallet} and upsert
     user_positions.

No OAuth, no bearer token — the data is public. That means no
encryption for the stored address either; treat it like a username.

The CLOB endpoint we use is ``GET https://clob.polymarket.com/positions?address=…``
(configurable via POLYMARKET_API_BASE for staging mocks).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

import httpx


log = logging.getLogger("portfolio.polymarket")


# Polygon addresses: 0x + 40 hex chars. Enforced tight so we never
# round-trip a garbage string through the JSON RPC.
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _api_base() -> str:
    return os.environ.get(
        "POLYMARKET_API_BASE", "https://clob.polymarket.com",
    ).rstrip("/")


def is_valid_address(wallet_address: str) -> bool:
    return bool(_ADDRESS_RE.match(wallet_address or ""))


def upsert_connection(user_id: int, wallet_address: str) -> None:
    """Store the user's wallet. Idempotent."""
    import db
    now = int(time.time())
    wallet = wallet_address.lower()
    with db.conn() as c:
        c.execute(
            "INSERT INTO polymarket_connections "
            "(user_id, wallet_address, connected_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  wallet_address = excluded.wallet_address, "
            "  connected_at = excluded.connected_at, "
            "  sync_error = NULL, "
            "  sync_error_count = 0",
            (user_id, wallet, now),
        )


def get_connection(user_id: int) -> Optional[dict]:
    import db
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM polymarket_connections WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


async def fetch_positions(wallet_address: str) -> list[dict]:
    """Fetch raw positions from Polymarket CLOB.

    Returns the raw JSON list. Callers normalise + upsert. Errors bubble
    up so the sync job can record them in ``sync_error``.
    """
    url = f"{_api_base()}/positions"
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        resp = await client.get(url, params={"address": wallet_address})
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict):  # some CLOB flavours wrap in an envelope
        data = data.get("data") or data.get("positions") or []
    return data if isinstance(data, list) else []


def _normalise(row: dict) -> Optional[dict]:
    """Normalise a raw CLOB position into our ``user_positions`` shape.

    Polymarket uses "conditionId" + "outcomeIndex" (0=YES, 1=NO) on
    legacy markets, and ``asset`` for the CLOB token ID on newer ones.
    We accept both and fall back to the market slug where available.
    """
    market_id = (
        row.get("slug")
        or row.get("market")
        or row.get("conditionId")
        or row.get("asset")
    )
    if not market_id:
        return None
    outcome_idx = row.get("outcomeIndex")
    side = row.get("side") or ("yes" if outcome_idx in (0, "0") else "no")
    side = str(side).lower()
    if side not in ("yes", "no"):
        return None
    shares = _float(row.get("size") or row.get("shares") or 0)
    entry = _float(row.get("avgPrice") or row.get("entry_price"))
    current = _float(row.get("curPrice") or row.get("current_price"))
    value = _float(row.get("currentValue")) or (shares * (current or 0))
    unrealised = _float(row.get("cashPnl"))
    return {
        "platform": "polymarket",
        "market_id": f"poly:{market_id}",
        "market_question": row.get("title") or row.get("question"),
        "side": side,
        "shares": shares,
        "entry_price": entry,
        "current_price": current,
        "position_value_usd": value,
        "unrealised_pnl_usd": unrealised,
        "realised_pnl_usd": _float(row.get("realisedPnl")) or 0.0,
        "opened_at": row.get("created_at_epoch"),
    }


def _float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def sync_positions(user_id: int) -> dict:
    """Fetch + upsert the user's Polymarket positions.

    Returns a summary dict for the caller (typically the sync job) to
    log: `{count, errors, wallet}`.
    """
    import db
    conn_row = get_connection(user_id)
    if not conn_row:
        return {"count": 0, "wallet": None, "error": "not_connected"}
    wallet = conn_row["wallet_address"]
    now = int(time.time())
    try:
        raw = await fetch_positions(wallet)
    except Exception as exc:
        with db.conn() as c:
            c.execute(
                "UPDATE polymarket_connections SET "
                "  sync_error = ?, "
                "  sync_error_count = sync_error_count + 1 "
                "WHERE user_id = ?",
                (str(exc)[:500], user_id),
            )
        log.warning("polymarket sync failed for user=%s: %s", user_id, exc)
        return {"count": 0, "wallet": wallet, "error": str(exc)}

    normalised = [n for n in (_normalise(r) for r in raw) if n]
    with db.conn() as c:
        # Clear rows not in the latest snapshot so a closed position
        # disappears from the dashboard. Matched by (user, platform)
        # only — Kalshi rows on the same user stay.
        c.execute(
            "DELETE FROM user_positions "
            "WHERE user_id = ? AND platform = 'polymarket'",
            (user_id,),
        )
        for n in normalised:
            c.execute(
                "INSERT OR REPLACE INTO user_positions "
                "(user_id, platform, market_id, market_question, side, "
                " shares, entry_price, current_price, position_value_usd, "
                " unrealised_pnl_usd, realised_pnl_usd, opened_at, last_synced_at) "
                "VALUES (?, 'polymarket', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, n["market_id"], n.get("market_question"),
                    n["side"], n["shares"], n.get("entry_price"),
                    n.get("current_price"), n.get("position_value_usd"),
                    n.get("unrealised_pnl_usd"), n.get("realised_pnl_usd") or 0.0,
                    n.get("opened_at"), now,
                ),
            )
        c.execute(
            "UPDATE polymarket_connections SET "
            "  last_synced_at = ?, sync_error = NULL, sync_error_count = 0 "
            "WHERE user_id = ?",
            (now, user_id),
        )
    return {"count": len(normalised), "wallet": wallet, "error": None}
