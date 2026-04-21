"""Per-user portfolio sync for Polymarket + Kalshi.

Shared between the live `/api/markets/portfolio` endpoint and the
background sync cron jobs, so there is exactly one implementation of:
  - deactivate-on-Kalshi-401
  - narve.ai signal enrichment per position
  - persistence to `user_positions`
  - stale-row pruning when the exchange stops reporting a position
"""

from __future__ import annotations

import logging
from typing import Optional

import db

from .portfolio_aggregator import get_combined_portfolio
from .portfolio_signals import enrich_positions
from .encryption import decrypt_token

log = logging.getLogger("gateway.portfolio_sync")


async def sync_user_portfolio(
    user_id: int,
    *,
    poly_client,
    kalshi_client,
    unified_markets_module,
    markets_cache_ttl: int = 300,
    only_platform: Optional[str] = None,
) -> dict:
    """Fetch one user's live portfolio, overlay narve.ai signals, persist.

    *only_platform* lets the Polymarket-only and Kalshi-only cron jobs
    skip the other exchange — we don't want the 15-min Kalshi sweep to
    also hit Polymarket 15 min after the 10-min sweep just did.
    """
    creds = db.get_all_market_credentials(user_id)

    poly_address = None
    kalshi_token = None
    for c in creds:
        if not c["is_active"]:
            continue
        if c["source"] == "polymarket":
            poly_address = c["polymarket_wallet_address"]
        elif c["source"] == "kalshi" and c["kalshi_token"]:
            kalshi_token = decrypt_token(c["kalshi_token"])

    if only_platform == "polymarket":
        kalshi_token = None
    elif only_platform == "kalshi":
        poly_address = None

    portfolio = await get_combined_portfolio(
        poly_client, kalshi_client,
        polymarket_address=poly_address,
        kalshi_token=kalshi_token,
    )

    if portfolio.get("kalshi", {}).get("error") == "token_expired":
        db.set_market_credential_active(user_id, "kalshi", False)
        portfolio["kalshi"]["is_active"] = False
    else:
        portfolio.setdefault("kalshi", {})["is_active"] = portfolio.get("kalshi", {}).get("connected", False)
    portfolio.setdefault("polymarket", {})["is_active"] = portfolio.get("polymarket", {}).get("connected", False)

    poly_positions = portfolio.get("polymarket", {}).get("positions", []) or []
    kalshi_positions = portfolio.get("kalshi", {}).get("positions", []) or []

    market_map: dict = {}
    try:
        markets = await unified_markets_module.fetch_unified_markets(
            poly_client, kalshi_client, cache_ttl=markets_cache_ttl,
        )
        enriched = unified_markets_module.enrich_markets_with_intelligence(markets)
        needed = {p.get("market_id") for p in poly_positions + kalshi_positions if p.get("market_id")}
        market_map = {m.id: m for m in enriched if m.id in needed}
    except Exception as exc:
        log.warning("portfolio signal enrichment failed for user %d: %s", user_id, exc)

    portfolio["polymarket"]["positions"] = enrich_positions(poly_positions, market_map)
    portfolio["kalshi"]["positions"] = enrich_positions(kalshi_positions, market_map)

    for platform, positions in (
        ("polymarket", portfolio["polymarket"]["positions"]),
        ("kalshi", portfolio["kalshi"]["positions"]),
    ):
        keep_keys: set[tuple[str, str]] = set()
        for p in positions:
            market_id = p.get("market_id") or ""
            side = (p.get("side") or "").lower()
            if not market_id or side not in ("yes", "no"):
                continue
            shares = float(p.get("shares") or 0)
            if shares <= 0:
                continue
            db.upsert_user_position(
                user_id=user_id,
                platform=platform,
                market_id=market_id,
                market_title=p.get("market_title") or market_id,
                side=side,
                shares=shares,
                avg_entry_price=float(p.get("avg_price") or 0),
                current_price=float(p.get("current_price") or 0),
                unrealised_pnl=float(p.get("pnl") or 0),
                position_value_usd=float(p.get("value") or 0),
            )
            keep_keys.add((market_id, side))
        if (platform == "polymarket" and poly_address) or (platform == "kalshi" and kalshi_token):
            db.prune_stale_positions(user_id, platform, keep_keys)

    return portfolio


def list_active_user_ids(platform: str) -> list[int]:
    """Users with an active credential for *platform*."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT DISTINCT user_id FROM user_market_credentials "
            "WHERE source = ? AND is_active = 1",
            (platform,),
        ).fetchall()
    return [int(r["user_id"]) for r in rows]


def refresh_prices_only(user_id: int, market_price_map: dict[str, float]) -> int:
    """Update `current_price`, `position_value_usd`, and `unrealised_pnl` on
    cached positions from a prebuilt {market_id: yes_price} map. Used by the
    2-minute price-refresh cron so we don't re-hit the exchange position
    APIs for each ticking price update. Returns rows updated."""
    import time as _t

    positions = db.get_user_positions(user_id)
    if not positions:
        return 0

    now = int(_t.time())
    updated = 0
    with db.conn() as c:
        for p in positions:
            market_id = p["market_id"]
            side = p["side"]
            shares = float(p["shares"] or 0)
            avg = float(p["avg_entry_price"] or 0)
            yes_price = market_price_map.get(market_id)
            if yes_price is None:
                continue
            current_price = yes_price if side == "yes" else (1.0 - yes_price)
            value = round(shares * current_price, 2)
            pnl = round((current_price - avg) * shares, 2)
            c.execute(
                "UPDATE user_positions SET current_price = ?, position_value_usd = ?, "
                "unrealised_pnl = ?, last_synced_at = ? "
                "WHERE user_id = ? AND platform = ? AND market_id = ? AND side = ?",
                (current_price, value, pnl, now,
                 user_id, p["platform"], market_id, side),
            )
            updated += 1
    return updated
