"""Universe screener with filters.

Filters the CoinGecko top-N universe by:
  - min market cap
  - 24h volume threshold
  - price-change windows (1h / 24h / 7d / 30d)
  - optional category filter
  - sort by any column

This is the equivalent of CoinMarketCap's "screener" page.
"""
from __future__ import annotations

from typing import Optional


SORTABLE = {
    "rank":            ("market_cap_rank",   "asc"),
    "price":           ("current_price",     "desc"),
    "market_cap":      ("market_cap",        "desc"),
    "volume":          ("total_volume",      "desc"),
    "change_1h":       ("change_1h",         "desc"),
    "change_24h":      ("change_24h",        "desc"),
    "change_7d":       ("change_7d",         "desc"),
    "change_30d":      ("change_30d",        "desc"),
    "ath_pct":         ("ath_change_pct",    "desc"),
}


def screen(
    coins: list[dict], *,
    min_market_cap: Optional[float] = None,
    min_volume: Optional[float] = None,
    max_price_change_24h: Optional[float] = None,
    min_price_change_24h: Optional[float] = None,
    search: Optional[str] = None,
    sort: str = "rank",
    order: str = "asc",
    limit: int = 100,
) -> list[dict]:
    rows = list(coins or [])

    if min_market_cap is not None:
        rows = [c for c in rows if (c.get("market_cap") or 0) >= min_market_cap]
    if min_volume is not None:
        rows = [c for c in rows if (c.get("total_volume") or 0) >= min_volume]
    if min_price_change_24h is not None:
        rows = [c for c in rows if (c.get("change_24h") or -999) >= min_price_change_24h]
    if max_price_change_24h is not None:
        rows = [c for c in rows if (c.get("change_24h") or 999) <= max_price_change_24h]
    if search:
        q = search.lower().strip()
        rows = [c for c in rows
                if q in (c.get("name") or "").lower() or q in (c.get("symbol") or "").lower()]

    field, default_order = SORTABLE.get(sort, ("market_cap_rank", "asc"))
    use_order = order if order in ("asc", "desc") else default_order
    rev = use_order == "desc"
    rows.sort(key=lambda r: (r.get(field) is None, r.get(field) if r.get(field) is not None else 0),
              reverse=rev)
    return rows[:max(1, min(limit, 500))]
