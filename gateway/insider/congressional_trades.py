"""US Congressional stock-trade disclosures.

Primary: Capitol Trades BFF API (public) — https://bff.capitoltrades.com/trades
Secondary: QuiverQuant public tier — used when Capitol Trades is down.

Both endpoints return the same transaction-level rows. We normalise to
the BaseFetcher schema, flagging committee membership for the correlator.

Poll cadence: every 6 hours (see jobs/insider_jobs.py). The endpoint is
paginated; we pull the first page (100 rows default) each run — anything
deeper than that means we've been down for >6h and staleness is the right
alarm to hit.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import AsyncIterator, Optional

from insider.base import BaseFetcher, COMMITTEE_SECTOR_MAP, SignalStrength, register_fetcher


log = logging.getLogger("insider.congress")


CAPITOL_TRADES_URL = "https://bff.capitoltrades.com/trades"
QUIVER_FALLBACK_URL = "https://api.quiverquant.com/beta/live/congresstrading"


@register_fetcher
class CongressionalTradesFetcher(BaseFetcher):
    source_name = "congressional_trades"

    async def _fetch_rows(self, limit: Optional[int] = None) -> AsyncIterator[dict]:
        rows: list[dict] = []
        try:
            import httpx
        except ImportError:
            log.warning("httpx not available — skipping congressional_trades fetch")
            return
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": self.user_agent}) as client:
                resp = await client.get(CAPITOL_TRADES_URL, params={"pageSize": limit or 100})
                if resp.status_code == 200:
                    payload = resp.json()
                    rows = payload.get("data") or []
                else:
                    log.warning("capitol trades returned %s", resp.status_code)
        except Exception as exc:
            log.warning("capitol trades fetch failed: %s", exc)
            rows = []

        for row in rows:
            try:
                normalised = self._normalise(row)
            except Exception:
                continue
            if normalised is None:
                continue
            yield normalised

    def _normalise(self, row: dict) -> Optional[dict]:
        ext_id = str(
            row.get("id")
            or row.get("transactionId")
            or row.get("uuid")
            or ""
        )
        if not ext_id:
            return None

        ticker = row.get("ticker") or (row.get("asset") or {}).get("ticker")
        actor = row.get("politician") or {}
        committees_raw = row.get("committees") or []

        amount_usd = row.get("amount") or row.get("value") or None
        event_at = _parse_iso(row.get("transactionDate") or row.get("transaction_date"))
        disclosed_at = _parse_iso(row.get("disclosureDate") or row.get("disclosure_date")) or int(_dt.datetime.utcnow().timestamp())

        delay_days = None
        if event_at and disclosed_at:
            delay_days = max(0.0, (disclosed_at - event_at) / 86400.0)

        committees = []
        relevant_sectors = []
        for c in committees_raw:
            c_key = _slug(str(c.get("name") if isinstance(c, dict) else c))
            committees.append(c_key)
            relevant_sectors.extend(COMMITTEE_SECTOR_MAP.get(c_key, []))

        return {
            "external_id": ext_id,
            "disclosed_at": int(disclosed_at),
            "event_at": int(event_at) if event_at else None,
            "actor_name": str(
                actor.get("fullName")
                or actor.get("name")
                or row.get("name")
                or "Unknown"
            ),
            "actor_role": row.get("chamber") or actor.get("chamber") or "congress",
            "ticker": (ticker or "").upper() if ticker else None,
            "company_name": row.get("company") or row.get("assetDescription"),
            "action": row.get("type") or row.get("txType") or "trade",
            "amount_usd": _num(amount_usd),
            "amount_shares": _num(row.get("shares")),
            "disclosure_delay_days": delay_days,
            "committees": committees,
            "relevant_sectors": sorted(set(relevant_sectors))[:10],
            "raw_payload": row,
            "signal_strength": None,  # Let BaseFetcher pick based on amount + delay.
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
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace("$", "").replace(",", "").strip()
    # Handle ranges like "$1,001 - $15,000" — take the midpoint.
    if "-" in s:
        parts = [p for p in s.split("-") if p]
        try:
            nums = [float(p.strip()) for p in parts]
            return sum(nums) / len(nums)
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _slug(s: str) -> str:
    s = s.lower().replace("the ", "").strip()
    for ch in (" ", "-"):
        s = s.replace(ch, "_")
    for key in COMMITTEE_SECTOR_MAP:
        if key in s:
            return key
    return s
