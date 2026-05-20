from __future__ import annotations
"""Silver Bulletin polling fallback for when 538 CSVs go dark.

538 was wound down in 2024; the legacy CSVs at projects.fivethirtyeight.com
remain available for now but their long-term life is uncertain. This module
provides a fallback that scrapes Silver Bulletin's public JSON snapshots.

Used by PollingAggregator only if the 538 CSV returns empty for *all*
poll types in a refresh cycle. Output schema matches the 538 normalized
record format so downstream consumers don't have to special-case the source.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from ._retry import fetch_json_with_retry

logger = logging.getLogger(__name__)

# Silver Bulletin's public race-snapshot JSON (URLs are guesses based on the
# public site structure — adjust when the production endpoint is known).
SB_SNAPSHOTS = {
    "senate": "https://www.natesilver.net/api/2026/senate/polls.json",
    "house": "https://www.natesilver.net/api/2026/house/polls.json",
    "governor": "https://www.natesilver.net/api/2026/governor/polls.json",
}


class SilverBulletinFallback:
    """Polling fallback. Identical interface to PollingAggregator._fetch_538_csv."""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def fetch_polls(self, poll_type: str) -> list[dict]:
        url = SB_SNAPSHOTS.get(poll_type)
        if not url:
            return []
        session = await self._get_session()
        data = await fetch_json_with_retry(
            session, url, timeout=20, source_label=f"sb-{poll_type}", max_attempts=2,
        )
        if not data:
            return []
        return self._normalize(data, poll_type)

    def _normalize(self, payload: dict | list, poll_type: str) -> list[dict]:
        """Convert a Silver Bulletin response into the same shape as 538 rows.

        SB's exact schema isn't public, so we handle two reasonable shapes:
          - {polls: [{state, candidate, pct, ...}]}
          - [{state, candidate, pct, ...}]
        Anything else returns an empty list (caller logs).
        """
        rows = []
        if isinstance(payload, dict):
            payload = payload.get("polls", [])
        if not isinstance(payload, list):
            return []
        for p in payload:
            try:
                rows.append({
                    "poll_type": poll_type,
                    "state": (p.get("state") or "National").upper(),
                    "candidate": (p.get("candidate") or p.get("answer") or "").strip(),
                    "party": (p.get("party") or "").strip(),
                    "percentage": float(p.get("pct") or p.get("percentage") or 0),
                    "pollster": (p.get("pollster") or p.get("sponsor") or "").strip(),
                    "sample_size": int(p.get("sample_size") or 0) or None,
                    "population": (p.get("population") or "").strip(),
                    "start_date": p.get("start_date") or "",
                    "end_date": p.get("end_date") or "",
                    "race_id": p.get("race_id") or "",
                    "source": "silver_bulletin",
                })
            except (TypeError, ValueError, KeyError):
                continue
        return rows
