"""Congressional stock trade fetcher (STOCK Act disclosures).

Fetches trades from the Capitol Trades API (free tier) and/or
direct Senate/House disclosure scraping.

Schedule: every 6 hours
Source handle prefix: "congress:{politician_name}"

LEGAL: All data is from mandatory STOCK Act public disclosures.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import httpx

from insider.base_fetcher import BaseInsiderFetcher

log = logging.getLogger("insider.congressional")

# Capitol Trades BFF API (free, no auth required for basic access)
CAPITOL_TRADES_API = "https://bff.capitoltrades.com"

# Committee → sector mapping for signal strength boosting
COMMITTEE_SECTOR_MAP = {
    "armed services": ["RTX", "LMT", "NOC", "BA", "GD", "defence", "military", "aerospace"],
    "finance": ["JPM", "GS", "BAC", "banking", "fintech", "crypto", "financial"],
    "banking": ["JPM", "GS", "BAC", "banking", "fintech", "financial"],
    "energy": ["XOM", "CVX", "NEE", "FSLR", "energy", "oil", "gas", "solar", "renewable"],
    "health": ["PFE", "MRNA", "JNJ", "ABBV", "pharma", "biotech", "health", "fda"],
    "intelligence": ["PLTR", "surveillance", "defence", "intelligence"],
    "commerce": ["AMZN", "GOOGL", "META", "tech", "telecom"],
    "agriculture": ["ADM", "BG", "agriculture", "food", "farming"],
    "foreign relations": ["defence", "sanctions", "diplomacy", "geopolitics"],
    "judiciary": ["tech", "antitrust", "regulation"],
}


class CongressionalTradesFetcher(BaseInsiderFetcher):
    """Fetches congressional stock trades from Capitol Trades API."""

    source_name = "congressional_trades"

    async def fetch(self) -> list[dict]:
        signals = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{CAPITOL_TRADES_API}/trades",
                    params={"page": 1, "pageSize": 50},
                    headers={"Accept": "application/json"},
                )
                if resp.status_code != 200:
                    log.warning("Capitol Trades API returned %d", resp.status_code)
                    return []

                data = resp.json()
                trades = data.get("data", [])

                for trade in trades:
                    try:
                        sig = self._parse_trade(trade)
                        if sig:
                            signals.append(sig)
                    except Exception as e:
                        log.warning("Failed to parse trade: %s", e)

        except httpx.RequestError as e:
            log.warning("Capitol Trades API request failed: %s", e)
        except Exception as e:
            log.exception("Congressional trades fetch error: %s", e)

        log.info("Congressional: fetched %d signals", len(signals))
        return signals

    def _parse_trade(self, trade: dict) -> Optional[dict]:
        """Parse a single trade from Capitol Trades API response."""
        politician = trade.get("politician", {})
        name = politician.get("fullName") or politician.get("firstName", "")
        if not name:
            return None

        # Build filing ID for dedup
        trade_id = trade.get("_txId") or trade.get("id") or ""
        if not trade_id:
            return None

        # Parse dates
        pub_date = trade.get("pubDate") or trade.get("filingDate") or ""
        tx_date = trade.get("txDate") or ""
        disclosed_ts = self._parse_ts(pub_date)
        tx_ts = self._parse_ts(tx_date) if tx_date else None

        delay = None
        if disclosed_ts and tx_ts:
            delay = max(0, (disclosed_ts - tx_ts) // 86400)

        # Parse amount
        amount = self._parse_amount(trade.get("amount") or trade.get("range") or "")

        # Parse action
        tx_type = (trade.get("txType") or trade.get("type") or "").lower()
        if "purchase" in tx_type or "buy" in tx_type:
            action = "bought"
        elif "sale" in tx_type or "sell" in tx_type:
            action = "sold"
        else:
            action = "disclosed"

        # Asset info
        asset = trade.get("asset", {})
        ticker = asset.get("assetTicker") or asset.get("ticker") or ""
        asset_name = asset.get("assetName") or asset.get("name") or ticker
        asset_display = f"{asset_name} ({ticker})" if ticker else asset_name

        # Politician details
        chamber = politician.get("chamber", "").lower()
        party = politician.get("party", "")
        state = politician.get("state", "")
        committees = politician.get("committees", [])
        committee_str = ", ".join(committees) if isinstance(committees, list) else str(committees)

        # Signal strength
        strength = self.calculate_signal_strength(
            amount_usd=amount,
            delay_days=delay,
            committees=committees,
            ticker=ticker,
            action=action,
        )

        return {
            "signal_type": "congressional_trade",
            "source_name": name,
            "source_type": "senator" if chamber == "senate" else "representative",
            "action": action,
            "asset_or_entity": asset_display,
            "amount_usd": amount,
            "disclosed_at": disclosed_ts or int(time.time()),
            "transaction_at": tx_ts,
            "delay_days": delay,
            "raw_data": json.dumps(trade, default=str),
            "signal_strength": strength,
            "filing_id": f"congress:{trade_id}",
            "committee": committee_str,
            "party": party,
            "state": state,
            "chamber": chamber,
        }

    def calculate_signal_strength(
        self,
        amount_usd: Optional[float] = None,
        delay_days: Optional[int] = None,
        **kwargs,
    ) -> str:
        """Congressional-specific strength with committee boosting."""
        base = super().calculate_signal_strength(amount_usd, delay_days)

        committees = kwargs.get("committees", [])
        ticker = (kwargs.get("ticker") or "").upper()
        action = kwargs.get("action", "")

        # Committee relevance boost
        if isinstance(committees, list):
            for committee in committees:
                committee_lower = committee.lower()
                for key, sectors in COMMITTEE_SECTOR_MAP.items():
                    if key in committee_lower:
                        if ticker in sectors or any(s in ticker.lower() for s in sectors):
                            if base == "weak":
                                return "moderate"
                            if base == "moderate":
                                return "strong"

        # Purchases are more significant than sales
        if action == "bought" and base == "weak":
            return "moderate"

        return base

    def is_available(self) -> bool:
        return True  # Capitol Trades API is free, no auth needed

    def _parse_ts(self, date_str: str) -> Optional[int]:
        """Parse ISO date string to unix timestamp."""
        if not date_str:
            return None
        try:
            from datetime import datetime, timezone
            # Handle various formats
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(date_str[:26], fmt)
                    return int(dt.replace(tzinfo=timezone.utc).timestamp())
                except ValueError:
                    continue
            return None
        except Exception:
            return None

    def _parse_amount(self, amount_str: str) -> Optional[float]:
        """Parse amount range string to midpoint USD value."""
        if not amount_str:
            return None
        # Capitol Trades returns ranges like "$1,001 - $15,000"
        amount_str = str(amount_str).replace("$", "").replace(",", "")
        parts = amount_str.split("-")
        try:
            if len(parts) == 2:
                low = float(parts[0].strip())
                high = float(parts[1].strip())
                return (low + high) / 2
            return float(parts[0].strip())
        except (ValueError, IndexError):
            return None
