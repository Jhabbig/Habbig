"""FEC campaign finance fetcher.

Fetches campaign contributions, PAC activity, and fundraising data from the
FEC API. Sudden donation surges to a candidate often precede polling moves
and prediction market repricing.

FEC API: https://api.open.fec.gov/v1/ (completely free, just need API key)

Schedule: daily (FEC data updates daily)
Source handle prefix: "fec:{candidate_name}"

LEGAL: FEC data is public by law (Federal Election Campaign Act).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import httpx

from insider.base_fetcher import BaseInsiderFetcher

log = logging.getLogger("insider.fec")

FEC_API_BASE = "https://api.open.fec.gov/v1"
FEC_API_KEY = os.environ.get("FEC_API_KEY", "DEMO_KEY")

# Candidates to monitor — those with active prediction markets
MONITORED_CANDIDATES = [
    # Add candidate IDs or names relevant to current election cycle
]


class FECCampaignFetcher(BaseInsiderFetcher):
    """Fetches FEC campaign finance data for donation surge detection."""

    source_name = "fec_campaign"

    async def fetch(self) -> list[dict]:
        signals = []

        if not FEC_API_KEY or FEC_API_KEY == "DEMO_KEY":
            log.info("FEC: using DEMO_KEY — limited rate. Set FEC_API_KEY for full access.")

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                # Fetch recent large individual contributions
                resp = await client.get(
                    f"{FEC_API_BASE}/schedules/schedule_a/",
                    params={
                        "api_key": FEC_API_KEY,
                        "sort": "-contribution_receipt_date",
                        "per_page": 50,
                        "min_amount": 10000,  # Only large contributions
                        "sort_hide_null": True,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for result in data.get("results", []):
                        try:
                            sig = self._parse_contribution(result)
                            if sig:
                                signals.append(sig)
                        except Exception as e:
                            log.warning("Failed to parse FEC contribution: %s", e)
                elif resp.status_code == 429:
                    log.warning("FEC API rate limited")
                else:
                    log.warning("FEC API returned %d", resp.status_code)

        except httpx.RequestError as e:
            log.warning("FEC API request failed: %s", e)
        except Exception as e:
            log.exception("FEC fetch error: %s", e)

        log.info("FEC: fetched %d signals", len(signals))
        return signals

    def _parse_contribution(self, result: dict) -> Optional[dict]:
        """Parse a single FEC contribution result."""
        sub_id = result.get("sub_id") or result.get("transaction_id") or ""
        if not sub_id:
            return None

        candidate_name = result.get("candidate_name") or result.get("committee", {}).get("name", "")
        contributor = result.get("contributor_name", "")
        amount = result.get("contribution_receipt_amount") or 0
        date_str = result.get("contribution_receipt_date", "")

        if not candidate_name or amount < 5000:
            return None

        disclosed_ts = self._parse_date(date_str)

        # Large donations (>$50k) are always significant for election markets
        if amount >= 50000:
            strength = "strong"
        elif amount >= 25000:
            strength = "moderate"
        else:
            strength = "weak"

        return {
            "signal_type": "fec_surge",
            "source_name": contributor or "Anonymous donor",
            "source_type": "pac" if "pac" in contributor.lower() else "individual",
            "action": "donated",
            "asset_or_entity": candidate_name,
            "amount_usd": float(amount),
            "disclosed_at": disclosed_ts or int(time.time()),
            "transaction_at": disclosed_ts,
            "delay_days": 0,
            "raw_data": json.dumps(result, default=str),
            "signal_strength": strength,
            "filing_id": f"fec:{sub_id}",
            "committee": None,
            "party": result.get("candidate_party", ""),
            "state": result.get("contributor_state", ""),
            "chamber": None,
        }

    def is_available(self) -> bool:
        return True  # FEC API always available (DEMO_KEY works)

    def _parse_date(self, date_str: str) -> Optional[int]:
        if not date_str:
            return None
        try:
            from datetime import datetime, timezone
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, AttributeError):
            return None
