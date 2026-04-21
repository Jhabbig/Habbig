"""SEC Form 13F — quarterly institutional holdings disclosures.

Polls daily. Form 13F is filed quarterly within 45 days of period-end,
so daily polling just keeps us near-real-time for the 3–5 filings that
land each day across the institutions we track.

Without a configured ``MONITORED_13F_CIKS`` env var this fetcher is a
no-op. Populate it with comma-separated CIK numbers for hedge funds or
family offices the product wants to track.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import AsyncIterator, Optional

from insider.base import BaseFetcher, register_fetcher


log = logging.getLogger("insider.sec_form13f")


@register_fetcher
class SecForm13FFetcher(BaseFetcher):
    source_name = "sec_form13f"
    user_agent = "narve.ai SEC-pipeline contact@narve.ai"

    async def _fetch_rows(self, limit: Optional[int] = None) -> AsyncIterator[dict]:
        ciks = [c.strip() for c in os.environ.get("MONITORED_13F_CIKS", "").split(",") if c.strip()]
        if not ciks:
            log.info("sec_form13f: no MONITORED_13F_CIKS configured; skip")
            return

        try:
            import httpx
        except ImportError:
            log.warning("httpx not installed — skipping sec_form13f")
            return

        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": self.user_agent}) as client:
            for cik in ciks[:25]:
                url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                except Exception as exc:
                    log.warning("sec_form13f fetch %s failed: %s", cik, exc)
                    continue

                filings = (data.get("filings", {}).get("recent") or {})
                forms = filings.get("form") or []
                acc = filings.get("accessionNumber") or []
                dates = filings.get("filingDate") or []

                for idx, form in enumerate(forms):
                    if form not in ("13F-HR", "13F-HR/A"):
                        continue
                    try:
                        accession = acc[idx]
                        disclosed = _parse_ymd(dates[idx])
                    except (IndexError, ValueError):
                        continue

                    yield {
                        "external_id": f"{cik}:{accession}",
                        "disclosed_at": disclosed or 0,
                        "event_at": disclosed,
                        "actor_name": data.get("name") or f"CIK {cik}",
                        "actor_role": "institutional_investor",
                        "ticker": None,
                        "company_name": None,
                        "action": form.lower(),
                        "amount_usd": None,
                        "amount_shares": None,
                        "disclosure_delay_days": None,
                        "committees": [],
                        "relevant_sectors": [],
                        "raw_payload": {"accession": accession, "filingDate": dates[idx]},
                    }


def _parse_ymd(val) -> Optional[int]:
    if not val:
        return None
    try:
        return int(_dt.datetime.strptime(str(val), "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp())
    except ValueError:
        return None
