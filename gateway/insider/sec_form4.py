"""SEC Form 4 insider trading filings fetcher.

Fetches insider transactions (executive stock buys/sells) from SEC EDGAR.
Form 4 filings must be submitted within 2 business days of the transaction.

EDGAR API: https://efts.sec.gov/LATEST/search-index?q=...
Full-text search: https://efts.sec.gov/LATEST/search-index?q="form 4"&dateRange=custom&startdt=...

Schedule: every 4 hours (Form 4s filed frequently)
Source handle prefix: "sec4:{company}:{insider_name}"

LEGAL: SEC EDGAR data is public domain. The SEC requires a descriptive
User-Agent header but no API key.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import httpx

from insider.base_fetcher import BaseInsiderFetcher

log = logging.getLogger("insider.sec_form4")

# SEC EDGAR requires a descriptive User-Agent (not a key — just identification)
SEC_USER_AGENT = os.environ.get(
    "SEC_EDGAR_USER_AGENT", "narve.ai contact@narve.ai"
)

# Tickers to monitor — companies with active prediction markets
MONITORED_TICKERS = [
    # Tech (AI/crypto markets)
    "NVDA", "MSFT", "GOOGL", "META", "AMZN", "TSLA", "COIN", "MSTR",
    # Defence (geopolitics markets)
    "RTX", "LMT", "NOC", "BA", "GD",
    # Energy (climate/energy markets)
    "XOM", "CVX", "NEE", "FSLR",
    # Pharma (health markets)
    "PFE", "MRNA", "JNJ", "ABBV",
    # Finance (economic markets)
    "JPM", "GS", "BAC",
]

# EDGAR full-text search endpoint (free, 10 req/sec with proper User-Agent)
EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"


class SECForm4Fetcher(BaseInsiderFetcher):
    """Fetches SEC Form 4 insider transaction filings from EDGAR."""

    source_name = "sec_form4"

    async def fetch(self) -> list[dict]:
        signals = []

        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": SEC_USER_AGENT},
        ) as client:
            # Fetch recent Form 4 filings via EDGAR full-text search
            try:
                resp = await client.get(
                    "https://efts.sec.gov/LATEST/search-index",
                    params={
                        "q": '"form 4"',
                        "dateRange": "custom",
                        "startdt": _days_ago_str(3),
                        "enddt": _today_str(),
                        "forms": "4",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for hit in data.get("hits", {}).get("hits", [])[:30]:
                        try:
                            sig = self._parse_filing(hit)
                            if sig:
                                signals.append(sig)
                        except Exception as e:
                            log.warning("Failed to parse Form 4: %s", e)
            except Exception as e:
                log.warning("EDGAR search failed: %s", e)

            # Also check specific monitored tickers via company filings
            for ticker in MONITORED_TICKERS[:10]:  # rate-limit friendly
                try:
                    sigs = await self._fetch_for_ticker(client, ticker)
                    signals.extend(sigs)
                    # SEC rate limit: 10 req/sec — be conservative
                    import asyncio
                    await asyncio.sleep(0.2)
                except Exception as e:
                    log.warning("Form 4 fetch for %s failed: %s", ticker, e)

        # Deduplicate by filing_id
        seen = set()
        unique = []
        for s in signals:
            fid = s.get("filing_id", "")
            if fid not in seen:
                seen.add(fid)
                unique.append(s)

        log.info("SEC Form 4: fetched %d unique signals", len(unique))
        return unique

    async def _fetch_for_ticker(self, client: httpx.AsyncClient, ticker: str) -> list[dict]:
        """Fetch recent Form 4 filings for a specific ticker."""
        # Note: EDGAR doesn't have a direct ticker search — we'd need to
        # map ticker → CIK first via the company tickers file. For the MVP,
        # we rely on the full-text search above. This method is a placeholder
        # for when we implement the CIK mapping.
        return []

    def _parse_filing(self, hit: dict) -> Optional[dict]:
        """Parse a single EDGAR search result into an InsiderSignal."""
        source = hit.get("_source", {})
        filing_id = source.get("file_num") or source.get("id") or ""
        if not filing_id:
            return None

        # Extract key fields
        entity_name = source.get("entity_name", "")
        display_names = source.get("display_names", [])
        insider_name = display_names[0] if display_names else entity_name

        file_date = source.get("file_date", "")
        period_of_report = source.get("period_of_report", "")

        disclosed_ts = self._parse_date(file_date)
        tx_ts = self._parse_date(period_of_report) if period_of_report else None

        delay = None
        if disclosed_ts and tx_ts:
            delay = max(0, (disclosed_ts - tx_ts) // 86400)

        # Determine action from form type and content
        form_type = source.get("form_type", "4")
        # Form 4 doesn't tell us buy/sell in the search result — we'd need
        # to parse the XML filing for that. For the MVP, default to "disclosed"
        action = "disclosed"

        strength = self.calculate_signal_strength(
            amount_usd=None,  # Amount requires XML parsing
            delay_days=delay,
        )

        return {
            "signal_type": "sec_form4",
            "source_name": insider_name,
            "source_type": "executive",
            "action": action,
            "asset_or_entity": entity_name,
            "amount_usd": None,
            "disclosed_at": disclosed_ts or int(time.time()),
            "transaction_at": tx_ts,
            "delay_days": delay,
            "raw_data": json.dumps(source, default=str),
            "signal_strength": strength,
            "filing_id": f"sec4:{filing_id}",
            "committee": None,
            "party": None,
            "state": None,
            "chamber": None,
        }

    def calculate_signal_strength(
        self,
        amount_usd: Optional[float] = None,
        delay_days: Optional[int] = None,
        **kwargs,
    ) -> str:
        """SEC Form 4 specific strength rules.

        Purchases (especially by C-suite) are the strongest signal.
        Sales are weaker because they're often scheduled 10b5-1 plans.
        """
        amount = amount_usd or 0
        delay = delay_days if delay_days is not None else 999

        # Cluster buying would be strong but requires batch analysis
        if amount >= 500000 and delay <= 5:
            return "strong"
        if amount >= 100000 or delay <= 10:
            return "moderate"
        return "weak"

    def is_available(self) -> bool:
        return True  # EDGAR is always available (public domain)

    def _parse_date(self, date_str: str) -> Optional[int]:
        if not date_str:
            return None
        try:
            from datetime import datetime, timezone
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, AttributeError):
            return None


def _days_ago_str(n: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def _today_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
