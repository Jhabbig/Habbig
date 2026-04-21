"""SEC Form 4 — corporate insider transactions.

SEC EDGAR requires a real User-Agent per their fair-use policy. We send:
  User-Agent: narve.ai contact@narve.ai

Polls every 4 hours for a configurable MONITORED_TICKERS list. The index
is CIK-keyed so we pre-resolve tickers via the EDGAR company-tickers JSON.

Running the fetcher without ``MONITORED_TICKERS`` configured is a no-op
rather than an error — the admin configures tickers in env once the
pipeline is live.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import AsyncIterator, Optional

from insider.base import BaseFetcher, register_fetcher


log = logging.getLogger("insider.sec_form4")


EDGAR_BASE = "https://data.sec.gov"
SUBMISSIONS_URL_TEMPLATE = f"{EDGAR_BASE}/submissions/CIK{{cik}}.json"


@register_fetcher
class SecForm4Fetcher(BaseFetcher):
    source_name = "sec_form4"
    user_agent = "narve.ai SEC-pipeline contact@narve.ai"

    async def _fetch_rows(self, limit: Optional[int] = None) -> AsyncIterator[dict]:
        tickers = [t.strip().upper() for t in os.environ.get("MONITORED_TICKERS", "").split(",") if t.strip()]
        if not tickers:
            log.info("sec_form4: no MONITORED_TICKERS configured; skip")
            return

        try:
            import httpx
        except ImportError:
            log.warning("httpx not installed — skipping sec_form4")
            return

        # Lightweight: one request per ticker (max 10 tickers). The SEC
        # rate limit is 10 req/s; we sleep 100ms between calls.
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": self.user_agent}) as client:
            for ticker in tickers[:10]:
                cik = await _cik_for_ticker(client, ticker, self.user_agent)
                if not cik:
                    continue
                await _async_sleep(0.1)
                url = SUBMISSIONS_URL_TEMPLATE.format(cik=cik.zfill(10))
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                except Exception as exc:
                    log.warning("sec_form4 fetch %s failed: %s", ticker, exc)
                    continue

                filings = (data.get("filings", {}).get("recent") or {})
                forms = filings.get("form") or []
                acc = filings.get("accessionNumber") or []
                dates = filings.get("filingDate") or []
                report_dates = filings.get("reportDate") or []

                for idx, form in enumerate(forms):
                    if form != "4":
                        continue
                    try:
                        accession = acc[idx]
                        disclosed = _parse_ymd(dates[idx])
                        event_at = _parse_ymd(report_dates[idx]) if idx < len(report_dates) else disclosed
                    except (IndexError, ValueError):
                        continue

                    delay_days = None
                    if disclosed and event_at:
                        delay_days = max(0.0, (disclosed - event_at) / 86400.0)

                    yield {
                        "external_id": f"{ticker}:{accession}",
                        "disclosed_at": disclosed or 0,
                        "event_at": event_at,
                        "actor_name": data.get("name") or ticker,
                        "actor_role": "corporate_insider",
                        "ticker": ticker,
                        "company_name": data.get("name"),
                        "action": "form_4",
                        "amount_usd": None,
                        "amount_shares": None,
                        "disclosure_delay_days": delay_days,
                        "committees": [],
                        "relevant_sectors": [],
                        "raw_payload": {"accession": accession, "filingDate": dates[idx]},
                    }


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _async_sleep(s: float) -> None:
    import asyncio
    await asyncio.sleep(s)


_cik_cache: dict[str, str] = {}


async def _cik_for_ticker(client, ticker: str, user_agent: str) -> Optional[str]:
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    try:
        resp = await client.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": user_agent},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None
    for entry in (data.values() if isinstance(data, dict) else []):
        if entry.get("ticker", "").upper() == ticker.upper():
            cik = str(entry.get("cik_str") or "")
            if cik:
                _cik_cache[ticker] = cik
                return cik
    return None


def _parse_ymd(val) -> Optional[int]:
    if not val:
        return None
    try:
        return int(_dt.datetime.strptime(str(val), "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp())
    except ValueError:
        return None
