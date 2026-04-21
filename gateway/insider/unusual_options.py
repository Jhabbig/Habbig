"""Unusual Whales — unusual options flow (free tier).

Free tier is rate-limited; we poll every 2 hours and request the most
recent page each run.
"""

from __future__ import annotations

import logging
import os
import time
from typing import AsyncIterator, Optional

from insider.base import BaseFetcher, SignalStrength, register_fetcher


log = logging.getLogger("insider.unusual_options")


UNUSUAL_WHALES_URL = "https://api.unusualwhales.com/api/flow/alerts"


@register_fetcher
class UnusualOptionsFetcher(BaseFetcher):
    source_name = "unusual_options"

    async def _fetch_rows(self, limit: Optional[int] = None) -> AsyncIterator[dict]:
        token = os.environ.get("UNUSUAL_WHALES_TOKEN", "")
        if not token:
            log.info("unusual_options: no UNUSUAL_WHALES_TOKEN; skip")
            return

        try:
            import httpx
        except ImportError:
            log.warning("httpx not installed — skipping unusual_options")
            return

        try:
            async with httpx.AsyncClient(timeout=20, headers={
                "User-Agent": self.user_agent,
                "Authorization": f"Bearer {token}",
            }) as client:
                resp = await client.get(UNUSUAL_WHALES_URL, params={"limit": limit or 100})
                if resp.status_code != 200:
                    log.warning("unusual_options returned %s", resp.status_code)
                    return
                data = resp.json()
        except Exception as exc:
            log.warning("unusual_options fetch failed: %s", exc)
            return

        rows = data.get("data") or data if isinstance(data, list) else []
        now = int(time.time())
        for row in rows:
            ext_id = str(row.get("id") or row.get("alert_id") or "")
            if not ext_id:
                continue
            ticker = (row.get("ticker") or row.get("symbol") or "").upper() or None
            premium = row.get("total_premium") or row.get("premium") or 0
            try:
                amount_usd = float(premium)
            except (TypeError, ValueError):
                amount_usd = 0.0

            # Unusual flow is disclosed effectively instantly.
            delay_days = 0.0
            strength = (SignalStrength.STRONG if amount_usd >= 500_000
                        else SignalStrength.MODERATE if amount_usd >= 100_000
                        else SignalStrength.WEAK)

            yield {
                "external_id": ext_id,
                "disclosed_at": now,
                "event_at": now,
                "actor_name": ticker or "unknown",
                "actor_role": "options_flow",
                "ticker": ticker,
                "company_name": None,
                "action": row.get("type") or "unusual_option_flow",
                "amount_usd": amount_usd,
                "amount_shares": None,
                "disclosure_delay_days": delay_days,
                "committees": [],
                "relevant_sectors": [],
                "signal_strength": strength,
                "raw_payload": row,
            }
