"""Shared contract for the six insider data fetchers.

Each fetcher subclass only implements ``_fetch_rows()`` — an async
generator yielding normalised dicts. The base class handles:

  - reading the ``insider_fetchers`` row for last_fetched / error state
  - de-duping against ``insider_signals(source, external_id)``
  - writing signal rows + housekeeping
  - emitting a FetchResult the scheduler can log

Fetchers must NOT import db.py. They open their own sqlite3 connection
(through _connect) so they stay independently testable.

Rate-limiting + User-Agent: each subclass sets its own headers. SEC
requires a contactable User-Agent; other APIs just want a sensible one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional


log = logging.getLogger("insider.base")


# Committee → sector heuristic map used by fetchers that expose committee
# membership. The correlator uses this as one input to signal_strength.
COMMITTEE_SECTOR_MAP: dict[str, list[str]] = {
    "armed_services": ["RTX", "LMT", "NOC", "BA", "GD", "HII", "defence"],
    "finance":        ["JPM", "GS", "BAC", "MS", "WFC", "fintech"],
    "energy":         ["XOM", "CVX", "COP", "SLB", "solar", "energy"],
    "health":         ["PFE", "MRNA", "JNJ", "UNH", "pharma", "biotech"],
    "intelligence":   ["defence", "cyber"],
    "transportation": ["BA", "DAL", "UAL", "UPS", "FDX"],
    "agriculture":    ["DE", "ADM", "agro"],
}


class SignalStrength:
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


@dataclass
class FetchResult:
    source: str
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    errors: int = 0
    error_message: Optional[str] = None
    duration_s: float = 0.0
    sample_external_ids: list[str] = field(default_factory=list)


# ── DB path (same convention as ai/cache.py) ────────────────────────────────


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ── BaseFetcher ──────────────────────────────────────────────────────────────


class BaseFetcher:
    """Abstract base — subclasses define source_name + _fetch_rows()."""

    source_name: str = "base"
    user_agent: str = "narve.ai insider-pipeline contact@narve.ai"

    async def _fetch_rows(self, limit: Optional[int] = None) -> AsyncIterator[dict]:
        """Yield normalised row dicts. Subclasses implement.

        Required keys:
          external_id, disclosed_at, actor_name, action
        Optional:
          event_at, actor_role, ticker, company_name, amount_usd, amount_shares,
          disclosure_delay_days, committees, relevant_sectors, raw_payload
        """
        if False:  # pragma: no cover — abstract
            yield {}
        raise NotImplementedError

    def default_strength(self, row: dict) -> str:
        """Cheap heuristic — subclasses may override."""
        amt = float(row.get("amount_usd") or 0)
        delay = float(row.get("disclosure_delay_days") or 999)
        if amt >= 250_000 and delay <= 7:
            return SignalStrength.STRONG
        if amt >= 50_000 or delay <= 30:
            return SignalStrength.MODERATE
        return SignalStrength.WEAK

    def amount_significance(self, row: dict) -> float:
        """0.0–1.0 scaled amount score — log-ish to keep $10M from dwarfing $100k."""
        amt = float(row.get("amount_usd") or 0)
        if amt <= 0:
            return 0.0
        import math
        # log10(1M) = 6 → 1.0, log10(1k) = 3 → 0.5.
        return max(0.0, min(1.0, (math.log10(max(amt, 1.0)) - 3.0) / 3.0))

    def disclosure_delay_score(self, row: dict) -> float:
        """Shorter delay → higher score. 0 days → 1.0, 45 days → 0.0."""
        delay = row.get("disclosure_delay_days")
        if delay is None:
            return 0.3  # unknown → below average but not zero
        d = max(0.0, float(delay))
        return max(0.0, min(1.0, 1.0 - (d / 45.0)))

    async def fetch_once(self, limit: Optional[int] = None) -> FetchResult:
        """Run one fetch cycle. Safe to call under cron."""
        start = time.monotonic()
        result = FetchResult(source=self.source_name)
        rows: list[dict] = []

        try:
            async for row in self._fetch_rows(limit=limit):
                rows.append(row)
                result.fetched += 1
        except Exception as exc:
            log.exception("insider.%s: fetch failed", self.source_name)
            result.errors += 1
            result.error_message = str(exc)[:500]

        # Persist
        try:
            conn = _connect()
            for row in rows:
                ext_id = str(row.get("external_id") or "")
                if not ext_id:
                    continue
                existing = conn.execute(
                    "SELECT id FROM insider_signals WHERE source=? AND external_id=?",
                    (self.source_name, ext_id),
                ).fetchone()
                if existing:
                    result.duplicates += 1
                    continue

                strength = row.get("signal_strength") or self.default_strength(row)
                conn.execute(
                    """
                    INSERT INTO insider_signals (
                        source, external_id, disclosed_at, event_at,
                        actor_name, actor_role, ticker, company_name,
                        action, amount_usd, amount_shares, raw_payload,
                        signal_strength, disclosure_delay_days,
                        amount_significance, committees, relevant_sectors,
                        narrative, fetched_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self.source_name, ext_id,
                        int(row.get("disclosed_at") or time.time()),
                        int(row.get("event_at") or 0) or None,
                        str(row.get("actor_name") or ""),
                        row.get("actor_role"),
                        row.get("ticker"),
                        row.get("company_name"),
                        row.get("action"),
                        row.get("amount_usd"),
                        row.get("amount_shares"),
                        json.dumps(row.get("raw_payload") or {})[:50000],
                        strength,
                        row.get("disclosure_delay_days"),
                        self.amount_significance(row),
                        json.dumps(row.get("committees") or []),
                        json.dumps(row.get("relevant_sectors") or []),
                        row.get("narrative"),
                        int(time.time()),
                    ),
                )
                result.inserted += 1
                if len(result.sample_external_ids) < 5:
                    result.sample_external_ids.append(ext_id)
            # Housekeeping
            conn.execute(
                """
                INSERT INTO insider_fetchers (
                    source, enabled, last_fetched_at, last_success_at,
                    last_error_at, last_error_message, consecutive_errors,
                    rows_fetched_total
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_fetched_at=excluded.last_fetched_at,
                    last_success_at = CASE WHEN ? = 0 THEN excluded.last_fetched_at
                                           ELSE last_success_at END,
                    last_error_at   = CASE WHEN ? = 0 THEN last_error_at
                                           ELSE excluded.last_error_at END,
                    last_error_message = CASE WHEN ? = 0 THEN last_error_message
                                               ELSE excluded.last_error_message END,
                    consecutive_errors = CASE WHEN ? = 0 THEN 0
                                               ELSE consecutive_errors + 1 END,
                    rows_fetched_total = rows_fetched_total + excluded.rows_fetched_total
                """,
                (
                    self.source_name,
                    int(time.time()),
                    int(time.time()) if result.errors == 0 else None,
                    int(time.time()) if result.errors else None,
                    result.error_message,
                    result.errors,
                    result.inserted,
                    result.errors, result.errors, result.errors, result.errors,
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            log.exception("insider.%s: persist failed", self.source_name)
            result.errors += 1

        result.duration_s = round(time.monotonic() - start, 3)
        return result


# Late-bound registry so subclasses can register themselves without
# ordering issues.
ALL_FETCHERS: dict[str, type[BaseFetcher]] = {}


def register_fetcher(cls: type[BaseFetcher]) -> type[BaseFetcher]:
    ALL_FETCHERS[cls.source_name] = cls
    return cls
