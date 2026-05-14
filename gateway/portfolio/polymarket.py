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

Market-state batching
---------------------
``fetch_market_state(market_ids)`` calls the Gamma batch endpoint
``GET /markets?id=a,b,c`` in chunks of up to 50 ids and memoises each
id's payload for ``_MARKET_CACHE_TTL`` seconds. Multiple users holding
the same market only generate one upstream request inside that window,
which is the single biggest API-call reduction in the sync job.

The CLOB ``/positions`` endpoint is still per-wallet — Polymarket does
not currently accept multiple addresses on that call (verified against
the public CLOB docs as of 2026-05-14). We document that limitation in
``fetch_positions`` and instead pace requests in the sync job.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Iterable, Optional

import httpx


log = logging.getLogger("portfolio.polymarket")


# Polygon addresses: 0x + 40 hex chars. Enforced tight so we never
# round-trip a garbage string through the JSON RPC.
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


# ── Market-state cache ────────────────────────────────────────────────────
#
# Gamma market data (price / liquidity / status) changes on the order of
# seconds, but the sync job runs every 10 minutes. Caching for 60s gives
# every user inside a given run window a coherent view while shielding
# Gamma from N duplicate requests for the same market.
_MARKET_CACHE_TTL = 60.0
_MARKET_CACHE_MAX = 5000  # hard cap so a memory leak never bites
_market_cache: dict[str, tuple[float, dict]] = {}

# Gamma accepts comma-separated ids on /markets. Polymarket has not
# published a hard cap; 50 is conservative enough that the URL stays well
# under typical 2KB limits and one bad batch only re-fetches 50 ids.
_GAMMA_BATCH_SIZE = 50


def _api_base() -> str:
    return os.environ.get(
        "POLYMARKET_API_BASE", "https://clob.polymarket.com",
    ).rstrip("/")


def _gamma_base() -> str:
    return os.environ.get(
        "POLYMARKET_GAMMA_BASE", "https://gamma-api.polymarket.com",
    ).rstrip("/")


def clear_market_cache() -> None:
    """Test helper — drop every cached market-state entry."""
    _market_cache.clear()


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

    Note: the CLOB ``/positions`` endpoint accepts exactly one
    ``address=`` parameter. There is no documented way to batch
    multiple wallets in a single request, so the sync job paces calls
    rather than batching them. If Polymarket exposes a multi-address
    filter in the future, this is the single call site to update.
    """
    url = f"{_api_base()}/positions"
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        resp = await client.get(url, params={"address": wallet_address})
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict):  # some CLOB flavours wrap in an envelope
        data = data.get("data") or data.get("positions") or []
    return data if isinstance(data, list) else []


async def fetch_market_state(
    market_ids: Iterable[str],
    *,
    now: Optional[float] = None,
) -> dict[str, dict]:
    """Fetch Gamma market state for many ids, batched + 60s cached.

    Returns a mapping ``{market_id: market_payload}`` for every id we
    successfully resolved. Cache hits avoid the network; cache misses
    are fetched in batches of ``_GAMMA_BATCH_SIZE`` using the Gamma
    multi-id endpoint ``GET /markets?id=a,b,c``.

    Failures on a batch are logged and the affected ids are simply
    omitted from the result — the caller already handles "no signal"
    for unknown markets. We never raise here so one slow Gamma response
    cannot kill the surrounding per-user sync loop.
    """
    now = now if now is not None else time.monotonic()
    out: dict[str, dict] = {}
    misses: list[str] = []

    # Cache lookup. Dedupes the input as a side effect.
    for mid in {m for m in market_ids if m}:
        cached = _market_cache.get(mid)
        if cached and (now - cached[0]) <= _MARKET_CACHE_TTL:
            out[mid] = cached[1]
        else:
            misses.append(mid)

    if not misses:
        return out

    # Batch network fetch. One client for all batches reuses the TCP
    # connection — cheaper than spinning up an httpx.AsyncClient per call.
    base = f"{_gamma_base()}/markets"
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        for i in range(0, len(misses), _GAMMA_BATCH_SIZE):
            chunk = misses[i : i + _GAMMA_BATCH_SIZE]
            try:
                resp = await client.get(
                    base, params={"id": ",".join(chunk)},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.warning(
                    "gamma batch fetch failed for %d ids: %s",
                    len(chunk), exc,
                )
                continue
            if isinstance(data, dict):
                data = data.get("data") or data.get("markets") or []
            if not isinstance(data, list):
                continue
            # Cache write uses the caller-supplied ``now`` so tests can
            # advance the clock without monkey-patching ``time.monotonic``.
            stamp = now
            for row in data:
                if not isinstance(row, dict):
                    continue
                # Gamma echoes the id back in a few shapes — prefer the
                # numeric ``id`` then ``conditionId`` then ``slug``.
                key = (
                    str(row.get("id"))
                    if row.get("id") is not None
                    else (row.get("conditionId") or row.get("slug"))
                )
                if not key:
                    continue
                _market_cache[key] = (stamp, row)
                out[key] = row

    # Cap the cache so a long-running worker doesn't grow without bound.
    if len(_market_cache) > _MARKET_CACHE_MAX:
        # Drop the oldest 25% — cheap and good enough for an in-process LRU.
        ordered = sorted(_market_cache.items(), key=lambda kv: kv[1][0])
        for k, _ in ordered[: len(ordered) // 4]:
            _market_cache.pop(k, None)

    return out


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
