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

        # SEC rate limit is 10 req/s; sleep 150ms between CIKs and back
        # off exponentially on 429/403. Accept-Encoding: gzip per SEC
        # fair-use recommendation.
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip",
        }
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
            for i, cik in enumerate(ciks[:25]):
                if i > 0:
                    await _async_sleep(0.15)
                url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
                data = await _get_with_backoff(client, url, cik)
                if data is None:
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


async def _async_sleep(s: float) -> None:
    import asyncio
    await asyncio.sleep(s)


async def _get_with_backoff(client, url: str, label: str) -> Optional[dict]:
    """Single GET with exponential backoff on SEC 429/403. Returns parsed
    JSON on success or None on permanent failure / non-JSON body."""
    for attempt in range(3):
        try:
            resp = await client.get(url)
        except Exception as exc:
            log.warning("sec_form13f fetch %s failed: %s", label, exc)
            return None
        if resp.status_code in (429, 403):
            await _async_sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except Exception:
            return None
    return None


def _parse_ymd(val) -> Optional[int]:
    if not val:
        return None
    try:
        return int(_dt.datetime.strptime(str(val), "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp())
    except ValueError:
        return None
