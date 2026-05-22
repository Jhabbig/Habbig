"""Polymarket source — surfaces active high-volume traders on markets that
match a dashboard's topic. Lead = a trader on a relevant market, where
"relevant" means the market's question text contains a topic keyword.

There is no DM channel on Polymarket itself, so these leads exist purely
to be cross-referenced manually (search the username on X or Discord).
The `url` points at the user's Polymarket profile.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx

from customer_bot.lead import RawLead

log = logging.getLogger("customer_bot.polymarket")

# Public Gamma API — no auth required for read.
MARKETS_URL = "https://gamma-api.polymarket.com/markets"
DATA_URL = "https://data-api.polymarket.com/trades"


async def fetch(client: httpx.AsyncClient, keywords: tuple[str, ...], limit: int = 20) -> AsyncIterator[RawLead]:
    try:
        r = await client.get(
            MARKETS_URL,
            params={"closed": "false", "active": "true", "limit": "200", "order": "volume24hr", "ascending": "false"},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        log.warning("Polymarket markets fetch failed: %s", exc)
        return
    if r.status_code != 200:
        log.warning("Polymarket markets returned %d", r.status_code)
        return
    try:
        markets = r.json() or []
    except ValueError:
        return
    if not isinstance(markets, list):
        return

    lowered = [k.lower() for k in keywords]
    relevant = []
    for m in markets:
        q = (m.get("question") or "").lower()
        if any(k in q for k in lowered):
            relevant.append(m)
            if len(relevant) >= 5:
                break

    seen: set[str] = set()
    for m in relevant:
        market_slug = m.get("slug") or m.get("conditionId") or ""
        if not market_slug:
            continue
        try:
            tr = await client.get(
                DATA_URL,
                params={"market": m.get("conditionId") or market_slug, "limit": "20"},
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            log.warning("Polymarket trades fetch failed for %s: %s", market_slug, exc)
            continue
        if tr.status_code != 200:
            continue
        try:
            trades = tr.json() or []
        except ValueError:
            continue
        for t in trades:
            addr = t.get("proxyWallet") or t.get("maker") or t.get("user") or ""
            if not addr or addr in seen:
                continue
            seen.add(addr)
            size_usd = 0.0
            try:
                size_usd = float(t.get("usdcSize") or t.get("size") or 0)
            except (TypeError, ValueError):
                pass
            if size_usd < 250:  # ignore dust; we want real traders
                continue
            yield RawLead(
                source="polymarket",
                source_id=f"polymarket:{market_slug}:{addr}",
                url=f"https://polymarket.com/profile/{addr}",
                author=addr,
                title=f"Active trader on: {m.get('question') or market_slug}",
                body=f"Trade size ${size_usd:,.0f} on market '{m.get('question')}'.",
                posted_at=0,
                engagement=int(size_usd),
                context_label="Polymarket",
            )
            if len(seen) >= limit:
                return
