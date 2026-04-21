"""Senate Lobbying Disclosure API — registered lobbying filings.

Public, free, no key required. Polls daily. We yield each newly-filed
LD-1/LD-2 form; the correlator maps ``client_name`` + ``issue_codes`` to
related sectors and markets.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import AsyncIterator, Optional

from insider.base import BaseFetcher, register_fetcher


log = logging.getLogger("insider.lobbying")


LDA_URL = "https://lda.senate.gov/api/v1/filings/"


@register_fetcher
class LobbyingFetcher(BaseFetcher):
    source_name = "lobbying"

    async def _fetch_rows(self, limit: Optional[int] = None) -> AsyncIterator[dict]:
        try:
            import httpx
        except ImportError:
            log.warning("httpx not installed — skipping lobbying")
            return

        try:
            async with httpx.AsyncClient(
                timeout=20, headers={"User-Agent": self.user_agent},
            ) as client:
                resp = await client.get(
                    LDA_URL,
                    params={
                        "ordering": "-dt_posted",
                        "page_size": min(limit or 100, 100),
                    },
                )
                if resp.status_code != 200:
                    log.warning("lobbying returned %s", resp.status_code)
                    return
                data = resp.json()
        except Exception as exc:
            log.warning("lobbying fetch failed: %s", exc)
            return

        for row in data.get("results") or []:
            ext_id = str(row.get("filing_uuid") or row.get("url") or "")
            if not ext_id:
                continue
            client_name = ((row.get("client") or {}).get("name") or "").strip()
            disclosed = _parse_iso(row.get("dt_posted") or row.get("filing_posted_date"))
            period = row.get("filing_period")

            yield {
                "external_id": ext_id,
                "disclosed_at": disclosed or int(_dt.datetime.utcnow().timestamp()),
                "event_at": None,
                "actor_name": client_name or "unknown",
                "actor_role": "lobbying_client",
                "ticker": None,
                "company_name": client_name,
                "action": f"lobbying:{period}" if period else "lobbying",
                "amount_usd": _num(row.get("income")) or _num(row.get("expenses")),
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
