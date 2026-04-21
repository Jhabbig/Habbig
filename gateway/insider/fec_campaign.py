"""FEC — US federal campaign contribution disclosures.

Uses the public OpenFEC API. Polls daily. Filings have a lag of hours
to a few days depending on the committee, so daily is enough.

Signals here aren't "trades" per se — they're political-action dollars.
The correlator uses them to flag markets about the candidate / PAC
receiving or sending money.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import AsyncIterator, Optional

from insider.base import BaseFetcher, register_fetcher


log = logging.getLogger("insider.fec")


FEC_BASE = "https://api.open.fec.gov/v1"


@register_fetcher
class FecCampaignFetcher(BaseFetcher):
    source_name = "fec_campaign"

    async def _fetch_rows(self, limit: Optional[int] = None) -> AsyncIterator[dict]:
        api_key = os.environ.get("FEC_API_KEY", "")
        if not api_key:
            log.info("fec_campaign: no FEC_API_KEY configured; skip")
            return

        try:
            import httpx
        except ImportError:
            log.warning("httpx not installed — skipping fec_campaign")
            return

        try:
            async with httpx.AsyncClient(
                timeout=20, headers={"User-Agent": self.user_agent},
                base_url=FEC_BASE,
            ) as client:
                resp = await client.get(
                    "/schedules/schedule_a/",
                    params={
                        "api_key": api_key,
                        "sort": "-contribution_receipt_date",
                        "per_page": min(limit or 100, 100),
                    },
                )
                if resp.status_code != 200:
                    log.warning("fec_campaign returned %s", resp.status_code)
                    return
                data = resp.json()
        except Exception as exc:
            log.warning("fec_campaign fetch failed: %s", exc)
            return

        for row in data.get("results") or []:
            ext_id = str(row.get("transaction_id") or row.get("sub_id") or "")
            if not ext_id:
                continue
            event_at = _parse_iso(row.get("contribution_receipt_date"))
            yield {
                "external_id": ext_id,
                "disclosed_at": int(_dt.datetime.utcnow().timestamp()),
                "event_at": event_at,
                "actor_name": row.get("contributor_name") or row.get("committee", {}).get("name") or "unknown",
                "actor_role": "campaign_contributor",
                "ticker": None,
                "company_name": row.get("contributor_employer"),
                "action": "contribution",
                "amount_usd": _num(row.get("contribution_receipt_amount")),
                "amount_shares": None,
                "disclosure_delay_days": None,
                "committees": [],
                "relevant_sectors": [],
                "raw_payload": row,
            }


def _parse_iso(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(_dt.datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _num(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
