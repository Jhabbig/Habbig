#!/usr/bin/env python3
"""
Polymarket → insider_events bridge.

The dashboard already runs a 30-min `suspicious_trades` scanner whose output
lives in memory at `server._last_sus_scan`. This module re-uses that work:
each flagged suspicious trade becomes an `insider_events` row with
venue='polymarket' and actor_label resolved through `wallet_labels`.

It also imports leaderboard pseudonyms into wallet_labels so PM traders'
own profile names ("Theo4", "Domer", "Fredi9999"…) auto-populate as
display names — no manual labeling needed for the long tail.

Why bridge instead of write-as-we-detect?
  - Keeps suspicious_trades.py decoupled from insider_events (the scanner
    is reusable for other consumers).
  - One-way fanout: the scanner is the source of truth; the bridge is the
    projection into the unified store.
  - Idempotent via UNIQUE(venue, source_id) — re-bridging the same scan is
    a no-op.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import insider_events
import wallet_labels

logger = logging.getLogger(__name__)

# Score floor — only mirror trades the scanner already decided are interesting
MIN_SCAN_SCORE = 25
# Cap rows per pass to keep the bridge cheap
MAX_ROWS_PER_PASS = 500


def _suspicious_trade_to_event(trade: dict) -> dict | None:
    """Map one row of `_last_sus_scan["suspicious_trades"]` → insider_events row."""
    wallet = (trade.get("wallet") or "").strip()
    if not wallet:
        return None

    # Stable source_id: tx_hash if present, else wallet+ts+market composite
    tx_hash = (trade.get("tx_hash") or "").strip()
    ts = int(trade.get("timestamp") or 0)
    market_id = (trade.get("market_id") or "").strip()
    if tx_hash:
        source_id = f"sus:{tx_hash}"
    else:
        source_id = f"sus:{wallet.lower()}:{market_id}:{ts}"

    # Resolve display name: manual label > polymarket pseudonym > wallet's own
    # `pseudonym/name` field (which we'll also import to wallet_labels) > short
    # address fallback
    label = wallet_labels.get_label(wallet)
    pseudonym = (trade.get("pseudonym") or trade.get("name") or "").strip()
    if label:
        actor_label = label["display_name"]
        actor_role = label.get("source", "polymarket").title()
    elif pseudonym:
        actor_label = pseudonym
        actor_role = "Polymarket trader"
    else:
        actor_label = wallet[:6] + "…" + wallet[-4:]
        actor_role = "Polymarket wallet"

    side_raw = (trade.get("side") or "").upper()
    side = {"BUY": "buy", "SELL": "sell"}.get(side_raw, "other")

    usd = float(trade.get("usd_value") or 0) or None
    price = float(trade.get("price") or 0) or None
    size = float(trade.get("size") or 0) or None
    score = trade.get("score") or 0
    reasons = trade.get("reasons") or []

    return {
        "venue": "polymarket",
        "source_id": source_id,
        "ts_filed": ts or None,         # PM has no separate filing — use trade ts
        "ts_executed": ts or None,
        "actor_id": wallet.lower(),
        "actor_label": actor_label,
        "actor_role": actor_role,
        # No real ticker — use the market's condition_id as the "symbol"-shaped
        # identifier. The unified feed renders it as a code; cross-venue
        # correlation skips PM rows because their symbol isn't a stock ticker.
        "symbol": (market_id[:12] or None) if market_id else None,
        "symbol_name": (trade.get("title") or "").strip() or None,
        "side": side,
        "shares": size,
        "price": price,
        "size_usd_low": usd,
        "size_usd_high": usd,
        "raw_url": (
            f"https://polymarket.com/event/{trade.get('slug')}"
            if trade.get("slug") else None
        ),
        "extra": {
            "outcome": trade.get("outcome"),
            "odds": trade.get("odds_str"),
            "potential_profit": trade.get("potential_profit"),
            "score": score,
            "reasons": reasons,
            "zscore": trade.get("zscore"),
            "tx_hash": tx_hash or None,
        },
    }


def _smart_money_to_events(flow: dict) -> list[dict]:
    """
    Map a smart-money flow row (consensus market) into N insider_events
    rows — one per top wallet in the consensus. Useful because consensus
    moves are a positioning signal even when no individual trade is sus.
    """
    market_id = (flow.get("market_id") or "").strip()
    if not market_id:
        return []
    title = flow.get("title") or ""
    outcome = flow.get("outcome") or ""
    end_date = flow.get("end_date") or ""

    # Use the wallets list from the flow row (already sorted desc by position)
    rows: list[dict] = []
    for w in (flow.get("wallets") or [])[:10]:
        addr = (w.get("address") or "").strip()
        if not addr:
            continue
        label = wallet_labels.get_label(addr)
        pseudonym = (w.get("pseudonym") or "").strip()
        actor_label = (label or {}).get("display_name") or pseudonym or (
            addr[:6] + "…" + addr[-4:]
        )
        actor_role = "Smart-money consensus"
        position_usd = float(w.get("position_usd") or 0) or None
        avg_price = float(w.get("avg_price") or 0) or None

        # source_id stable across passes: market+wallet
        source_id = f"smart:{market_id}:{addr.lower()}"
        rows.append({
            "venue": "polymarket",
            "source_id": source_id,
            "ts_filed": int(time.time()),  # consensus snapshot time
            "ts_executed": None,
            "actor_id": addr.lower(),
            "actor_label": actor_label,
            "actor_role": actor_role,
            "symbol": market_id[:12],
            "symbol_name": f"[Consensus·{outcome}] {title}",
            "side": "buy",  # consensus = positioned long the outcome
            "shares": None,
            "price": avg_price,
            "size_usd_low": position_usd,
            "size_usd_high": position_usd,
            "raw_url": (
                f"https://polymarket.com/event/{flow.get('slug')}"
                if flow.get("slug") else None
            ),
            "extra": {
                "outcome": outcome,
                "consensus_wallet_count": flow.get("smart_wallet_count"),
                "consensus_total_usd": flow.get("total_position_usd"),
                "avg_quality": flow.get("avg_quality"),
                "end_date": end_date,
                "wallet_quality_score": w.get("quality_score"),
            },
        })
    return rows


def import_leaderboard_pseudonyms(traders: list[dict]) -> dict:
    """
    Walk a Polymarket leaderboard payload (list of trader dicts with
    `proxyWallet` + `pseudonym`/`name`) and seed wallet_labels with the
    on-platform display name. Manual labels are never overwritten.
    """
    rows = []
    for t in traders:
        addr = t.get("proxyWallet") or t.get("address")
        name = t.get("pseudonym") or t.get("name")
        if addr and name:
            rows.append({"address": addr, "pseudonym": name})
    return wallet_labels.import_polymarket_pseudonyms(rows)


def run_bridge(scan: dict | None) -> dict:
    """
    Project the latest suspicious-trade scan into insider_events.

    `scan` is the dict cached as server._last_sus_scan. Returns a summary
    dict with how many rows were inserted vs deduped.
    """
    if not scan:
        return {"ok": False, "reason": "no scan available"}
    insider_events.init_db()
    wallet_labels.init_db()

    rows: list[dict] = []

    # 1) High-score suspicious trades
    for t in (scan.get("suspicious_trades") or [])[:MAX_ROWS_PER_PASS]:
        if (t.get("score") or 0) < MIN_SCAN_SCORE:
            continue
        row = _suspicious_trade_to_event(t)
        if row:
            rows.append(row)

    # 2) Smart-money consensus markets — one row per top wallet per market
    for flow in (scan.get("smart_money", {}).get("flows") or [])[:50]:
        rows.extend(_smart_money_to_events(flow))
        if len(rows) >= MAX_ROWS_PER_PASS * 2:
            break

    if not rows:
        return {"ok": True, "rows_built": 0, "inserted": 0, "skipped": 0, "errors": 0}

    res = insider_events.upsert_many(rows)
    return {
        "ok": True,
        "rows_built": len(rows),
        **res,
    }


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    # Standalone smoke run with a tiny synthetic scan
    fake_scan = {
        "suspicious_trades": [{
            "wallet": "0x" + "ab" * 20,
            "tx_hash": "0xdeadbeef",
            "timestamp": int(time.time()) - 3600,
            "market_id": "0xCONDITION1",
            "title": "Will X happen?",
            "outcome": "Yes",
            "side": "BUY",
            "size": 5000,
            "usd_value": 250,
            "price": 0.05,
            "potential_profit": 4750,
            "score": 80,
            "reasons": ["Long-shot", "New wallet"],
            "pseudonym": "TestWhale",
            "slug": "will-x-happen",
        }],
        "smart_money": {"flows": []},
    }
    print(json.dumps(run_bridge(fake_scan), indent=2))
