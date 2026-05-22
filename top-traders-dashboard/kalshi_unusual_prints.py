#!/usr/bin/env python3
"""
Kalshi unusual-prints ingester.

Kalshi has no per-trader public identity (KYC platform), so we can't track
"who" trades — but we can track "what looks abnormal." This module scans
the public Kalshi trades feed for prints that are large/loud relative to
each market's recent baseline and lands them as `venue='kalshi'` rows in
the unified insider_events store.

Heuristic per market:
  - Pull last N trades (default 200) via /markets/trades
  - Compute median trade `count` and a robust upper threshold
    (median + UNUSUAL_SIGMA × MAD-equivalent, floored at MIN_PRINT_COUNT)
  - Any trade whose `count` exceeds the threshold AND whose dollar value
    exceeds MIN_PRINT_USD is a flagged "unusual print"

Each flagged trade becomes one insider_events row:
  venue        = 'kalshi'
  source_id    = 'kalshi:{trade_id}' (idempotent across re-runs)
  actor_id     = None       # platform doesn't expose trader identity
  actor_label  = 'Kalshi anon · {ticker}'
  actor_role   = 'Kalshi unusual print'
  symbol       = event_ticker (truncated to fit insider_events.symbol)
  symbol_name  = market title
  side         = 'buy' if taker bought YES, 'sell' if taker sold YES, else 'other'
  shares       = trade.count
  price        = trade.yes_price (0–1 dollars)
  size_usd_*   = count × price (rough USD notional)
  raw_url      = https://kalshi.com/markets/{event_ticker}/{ticker}
  extra        = full trade payload for forensic inspection

This feeds the same correlation engine: a Kalshi market that prints big
right before a Form 4 filing on the same ticker becomes a candidate row
in /api/insider-correlations.
"""

from __future__ import annotations

import logging
import statistics
import time
from typing import Any

import httpx

import insider_events
import kalshi_client

logger = logging.getLogger(__name__)

KALSHI_HOST = kalshi_client.KALSHI_HOST
KALSHI_API_BASE = kalshi_client.KALSHI_API_BASE

# Tuning knobs — conservative defaults; the goal is "rare and interesting,"
# not "every above-average trade."
TRADES_PER_MARKET = 200
MIN_PRINT_COUNT = 500          # absolute floor on contracts so we ignore noise
MIN_PRINT_USD = 250.0          # floor on notional; sub-$250 prints rarely matter
UNUSUAL_SIGMA = 3.0            # how many MADs above the median is "unusual"
MAX_MARKETS_PER_PASS = 80      # cap so a pass stays under a couple of minutes
HTTP_TIMEOUT = 15.0
RATE_PAUSE = 0.10              # be a polite client


# ─── Kalshi public trades fetch ──────────────────────────────────────

def _fetch_trades_for_market(client: httpx.Client, ticker: str) -> list[dict]:
    """GET /trade-api/v2/markets/trades?ticker=… — returns raw trade dicts."""
    if not ticker:
        return []
    try:
        r = client.get(
            f"{KALSHI_HOST}{KALSHI_API_BASE}/markets/trades",
            params={"ticker": ticker, "limit": TRADES_PER_MARKET},
            headers={"Accept": "application/json", "User-Agent": "PolymarketTopTraders/1.0"},
        )
        if r.status_code != 200:
            return []
        data = r.json() or {}
        return data.get("trades") or []
    except Exception as e:
        logger.debug("kalshi trades fetch failed for %s: %s", ticker, e)
        return []


# ─── Unusual-print classifier ────────────────────────────────────────

def _is_unusual(count: float, baseline_median: float, baseline_mad: float) -> bool:
    """Robust outlier check using median + scaled MAD (works even on skewed dists)."""
    if count < MIN_PRINT_COUNT:
        return False
    # Scale MAD up to "stddev-ish" by 1.4826 — standard normal approximation
    threshold = baseline_median + UNUSUAL_SIGMA * (baseline_mad * 1.4826)
    return count >= threshold


def _trade_to_event(t: dict, market: dict) -> dict | None:
    trade_id = (t.get("trade_id") or "").strip()
    count = float(t.get("count") or 0)
    if count <= 0:
        return None

    # Kalshi v2 trade payload uses cents for prices on /markets/trades
    yes_price_raw = t.get("yes_price")
    if yes_price_raw is None:
        yes_price_raw = t.get("price")
    yes_price = kalshi_client._normalize_price(yes_price_raw)

    # Notional: contracts × price (each contract pays $1 if it resolves true)
    notional = round(count * yes_price, 2)
    if notional < MIN_PRINT_USD:
        return None

    # Side: which side was the *aggressor* (taker)?
    taker = (t.get("taker_side") or t.get("side") or "").lower()
    if taker == "yes":
        side = "buy"     # taker bought YES → market got pushed up
    elif taker == "no":
        side = "sell"    # taker bought NO  → market got pushed down on YES
    else:
        side = "other"

    ticker = market.get("ticker") or t.get("ticker") or ""
    event_ticker = market.get("event_ticker") or ""
    title = market.get("title") or market.get("event_title") or ticker

    # Stable source_id — Kalshi trade_ids are unique platform-wide
    if trade_id:
        source_id = f"kalshi:{trade_id}"
    else:
        # Fallback: synthesize from ticker + ts + count
        ts_str = str(t.get("created_time") or "")
        source_id = f"kalshi:{ticker}:{ts_str}:{count}"

    # Parse created_time (ISO 8601) → unix
    ts = 0
    created_time = t.get("created_time")
    if isinstance(created_time, str) and created_time:
        try:
            from datetime import datetime
            # Kalshi sends e.g. "2025-01-15T18:42:11.123Z"
            ts = int(datetime.fromisoformat(created_time.replace("Z", "+00:00")).timestamp())
        except Exception:
            ts = 0

    # Symbol field on insider_events is short (we use 12-char prefix). Use the
    # event_ticker as the cross-venue key — Kalshi event_tickers like
    # "KXNVDA-25Q1" carry the underlying so a Form 4 NVDA event can correlate.
    symbol_short = (event_ticker or ticker)[:12].upper() or None

    return {
        "venue": "kalshi",
        "source_id": source_id,
        "ts_filed": ts or None,
        "ts_executed": ts or None,
        "actor_id": None,
        "actor_label": f"Kalshi anon · {ticker[:24]}" if ticker else "Kalshi anon",
        "actor_role": "Kalshi unusual print",
        "symbol": symbol_short,
        "symbol_name": title[:120] if title else None,
        "side": side,
        "shares": count,
        "price": yes_price or None,
        "size_usd_low": notional or None,
        "size_usd_high": notional or None,
        "raw_url": (
            f"https://kalshi.com/markets/{event_ticker}/{ticker}"
            if event_ticker and ticker else None
        ),
        "extra": {
            "trade_id": trade_id or None,
            "kalshi_ticker": ticker,
            "event_ticker": event_ticker,
            "taker_side": taker or None,
            "yes_price": yes_price,
            "no_price": kalshi_client._normalize_price(t.get("no_price")),
            "count": count,
            "created_time": created_time,
            "category": market.get("category"),
        },
    }


# ─── Pass orchestration ──────────────────────────────────────────────

def run_ingest(max_markets: int = MAX_MARKETS_PER_PASS) -> dict:
    """
    Walk the top Kalshi markets, score each for unusual prints, write
    flagged trades to insider_events. Returns a summary dict.

    Idempotent: re-running on the same trades is a no-op via UNIQUE(venue, source_id).
    """
    insider_events.init_db()

    # Top markets by 24h volume — these are where actual flow happens.
    # Tiny illiquid markets generate noise that swamps the baseline.
    try:
        markets = kalshi_client.fetch_top_markets(limit=max_markets)
    except Exception as e:
        logger.warning("kalshi top-markets fetch failed: %s", e)
        return {"ok": False, "reason": f"top-markets fetch failed: {e}"}

    if not markets:
        return {"ok": True, "markets_scanned": 0, "trades_flagged": 0, "inserted": 0}

    flagged_rows: list[dict] = []
    markets_scanned = 0
    markets_with_flags = 0

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        for m in markets[:max_markets]:
            ticker = m.get("ticker")
            if not ticker:
                continue
            trades = _fetch_trades_for_market(client, ticker)
            time.sleep(RATE_PAUSE)
            if len(trades) < 10:
                # Not enough samples for a meaningful baseline
                continue
            markets_scanned += 1

            counts = [float(t.get("count") or 0) for t in trades if (t.get("count") or 0) > 0]
            if len(counts) < 10:
                continue
            try:
                med = statistics.median(counts)
                mad = statistics.median([abs(c - med) for c in counts]) or 1.0
            except statistics.StatisticsError:
                continue

            local_flags = 0
            for t in trades:
                count = float(t.get("count") or 0)
                if not _is_unusual(count, med, mad):
                    continue
                row = _trade_to_event(t, m)
                if row:
                    flagged_rows.append(row)
                    local_flags += 1
            if local_flags:
                markets_with_flags += 1

    if not flagged_rows:
        return {
            "ok": True,
            "markets_scanned": markets_scanned,
            "markets_with_flags": 0,
            "trades_flagged": 0,
            "inserted": 0, "skipped": 0, "errors": 0,
        }

    res = insider_events.upsert_many(flagged_rows)
    return {
        "ok": True,
        "markets_scanned": markets_scanned,
        "markets_with_flags": markets_with_flags,
        "trades_flagged": len(flagged_rows),
        **res,
    }


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run_ingest(max_markets=20), indent=2))
