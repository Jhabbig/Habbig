"""On-chain whale intelligence layer (F14).

Tracks large Polymarket positions via the Gamma API and correlates them
with narve.ai's credibility intelligence. Surfaces convergence (whale +
credible sources agree) and divergence (they disagree) signals.

Whale tiers:
  - $5K+    (small whale)
  - $25K+   (medium whale)
  - $100K+  (large whale)
  - $500K+  (mega whale)
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("markets.whale_tracker")

WHALE_TIERS = [
    (500_000, "500k"),
    (100_000, "100k"),
    (25_000, "25k"),
    (5_000, "5k"),
]


def classify_tier(amount_usd: float) -> Optional[str]:
    for threshold, label in WHALE_TIERS:
        if amount_usd >= threshold:
            return label
    return None


def hash_wallet(address: str) -> str:
    """Hash wallet address for privacy — we never store raw addresses."""
    return hashlib.sha256(address.encode()).hexdigest()[:16]


def get_whale_wallets() -> list[str]:
    """Return the list of whale wallet addresses to track.

    Loaded from WHALE_WALLETS env var (comma-separated) or defaults to
    an empty list (operator must configure).
    """
    raw = os.environ.get("WHALE_WALLETS", "").strip()
    if not raw:
        return []
    return [w.strip() for w in raw.split(",") if w.strip()]


async def poll_whale_positions() -> dict[str, Any]:
    """Fetch positions for tracked wallets and store new large positions.

    Returns: {new_positions: int, wallets_checked: int}
    """
    import db
    from backend.markets.polymarket_client import PolymarketClient

    wallets = get_whale_wallets()
    if not wallets:
        return {"new_positions": 0, "wallets_checked": 0, "reason": "no wallets configured"}

    poly = PolymarketClient()
    new_count = 0
    now = int(time.time())

    for address in wallets:
        try:
            positions = await poly.get_positions(address)
            if not positions:
                continue

            wallet_h = hash_wallet(address)

            for pos in positions:
                # Position structure varies — try common field names
                market_slug = pos.get("slug", pos.get("market", ""))
                if not market_slug:
                    continue

                # Calculate position value
                amount = float(pos.get("currentValue", 0) or pos.get("value", 0) or 0)
                if amount < 5000:
                    continue

                tier = classify_tier(amount)
                if not tier:
                    continue

                side = "YES" if float(pos.get("outcomeIndex", 0)) == 0 else "NO"
                market_id = f"poly:{market_slug}"

                # Only store if this is a new or significantly changed position
                with db.conn() as c:
                    existing = c.execute(
                        "SELECT amount_usd FROM whale_positions "
                        "WHERE wallet_hash = ? AND market_id = ? "
                        "ORDER BY detected_at DESC LIMIT 1",
                        (wallet_h, market_id),
                    ).fetchone()

                # Skip if position hasn't changed significantly (>20%)
                if existing and abs(amount - existing["amount_usd"]) / max(existing["amount_usd"], 1) < 0.20:
                    continue

                with db.conn() as c:
                    c.execute(
                        "INSERT INTO whale_positions (wallet_hash, market_id, side, amount_usd, tier, detected_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (wallet_h, market_id, side, amount, tier, now),
                    )
                new_count += 1

        except Exception as e:
            log.warning("Whale poll failed for wallet %s...: %s", address[:8], e)

    try:
        await poly.close()
    except Exception:
        pass

    return {"new_positions": new_count, "wallets_checked": len(wallets)}


def get_whale_activity_for_market(market_id: str, days: int = 7) -> list[dict]:
    """Get recent whale positions for a market."""
    import db
    cutoff = int(time.time()) - days * 86400
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM whale_positions WHERE market_id = ? AND detected_at >= ? "
            "ORDER BY amount_usd DESC",
            (market_id, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def get_whale_intelligence_for_market(market_id: str) -> dict:
    """Compute convergence/divergence between whale bets and credibility intelligence.

    Returns:
        whale_direction: "YES" | "NO" | "SPLIT" | None
        whale_total_usd: total whale capital
        convergence: "converge" | "diverge" | "neutral" | None
    """
    import db

    whales = get_whale_activity_for_market(market_id, days=7)
    if not whales:
        return {"whale_direction": None, "whale_total_usd": 0, "convergence": None}

    yes_capital = sum(w["amount_usd"] for w in whales if w["side"] == "YES")
    no_capital = sum(w["amount_usd"] for w in whales if w["side"] == "NO")
    total = yes_capital + no_capital

    if yes_capital > no_capital * 1.5:
        whale_dir = "YES"
    elif no_capital > yes_capital * 1.5:
        whale_dir = "NO"
    else:
        whale_dir = "SPLIT"

    # Get betyc consensus
    preds = db.get_predictions_for_market(market_id)
    betyc_consensus = None
    if preds:
        pred_dicts = [
            {
                "source_handle": p["source_handle"],
                "direction": p["direction"],
                "predicted_probability": p["predicted_probability"],
                "global_credibility": p["global_credibility"],
                "accuracy_unlocked": bool(p.get("accuracy_unlocked")),
            }
            for p in preds
        ]
        result = db.calculate_betyc_probability(pred_dicts)
        betyc_yes = result.get("betyc_yes_probability")
        if betyc_yes is not None:
            if betyc_yes > 0.55:
                betyc_consensus = "YES"
            elif betyc_yes < 0.45:
                betyc_consensus = "NO"
            else:
                betyc_consensus = "SPLIT"

    # Determine convergence
    convergence = None
    if whale_dir and betyc_consensus:
        if whale_dir == betyc_consensus:
            convergence = "converge"
        elif whale_dir == "SPLIT" or betyc_consensus == "SPLIT":
            convergence = "neutral"
        else:
            convergence = "diverge"

    return {
        "whale_direction": whale_dir,
        "whale_total_usd": round(total, 2),
        "whale_yes_usd": round(yes_capital, 2),
        "whale_no_usd": round(no_capital, 2),
        "whale_positions": len(whales),
        "betyc_consensus": betyc_consensus,
        "convergence": convergence,
    }
