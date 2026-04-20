"""Abstract base class for insider data fetchers.

Each fetcher connects to a single public disclosure data source and
produces InsiderSignal dicts that flow into the correlation engine.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

log = logging.getLogger("insider")


class BaseInsiderFetcher(ABC):
    """Base class for all insider data source fetchers."""

    source_name: str = ""  # Override: "congressional_trades", "sec_form4", etc.

    @abstractmethod
    async def fetch(self) -> list[dict]:
        """Fetch new insider signals from the data source.

        Returns a list of signal dicts matching the insider_signals schema:
          signal_type, source_name, source_type, action, asset_or_entity,
          amount_usd, disclosed_at, transaction_at, delay_days, raw_data,
          signal_strength, filing_id, committee, party, state, chamber
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """True if the fetcher is configured (API keys set, etc.)."""
        ...

    def calculate_signal_strength(
        self,
        amount_usd: Optional[float],
        delay_days: Optional[int],
        **kwargs,
    ) -> str:
        """Base signal strength calculation. Override for source-specific rules."""
        amount = amount_usd or 0
        delay = delay_days if delay_days is not None else 999

        if amount >= 50000 and delay <= 10:
            return "strong"
        if amount >= 15000 or delay <= 20:
            return "moderate"
        return "weak"


def store_signals(signals: list[dict]) -> int:
    """Store new insider signals in the database. Deduplicates by filing_id.

    Returns the number of new signals stored.
    """
    import db

    stored = 0
    now = int(time.time())

    for sig in signals:
        filing_id = sig.get("filing_id")
        if not filing_id:
            continue

        try:
            with db.conn() as c:
                c.execute(
                    """INSERT OR IGNORE INTO insider_signals
                        (signal_type, source_name, source_type, action,
                         asset_or_entity, amount_usd, disclosed_at,
                         transaction_at, delay_days, raw_data, fetched_at,
                         signal_strength, filing_id, committee, party,
                         state, chamber)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sig.get("signal_type", "unknown"),
                        sig.get("source_name", ""),
                        sig.get("source_type", ""),
                        sig.get("action", ""),
                        sig.get("asset_or_entity", ""),
                        sig.get("amount_usd"),
                        sig.get("disclosed_at", now),
                        sig.get("transaction_at"),
                        sig.get("delay_days"),
                        sig.get("raw_data"),
                        now,
                        sig.get("signal_strength", "weak"),
                        filing_id,
                        sig.get("committee"),
                        sig.get("party"),
                        sig.get("state"),
                        sig.get("chamber"),
                    ),
                )
                if c.execute("SELECT changes()").fetchone()[0] > 0:
                    stored += 1
        except Exception as e:
            log.warning("Failed to store signal %s: %s", filing_id, e)

    return stored


def update_fetcher_state(source: str, records: int, error: Optional[str] = None) -> None:
    """Update the insider_fetchers bookkeeping table."""
    import db

    now = int(time.time())
    with db.conn() as c:
        c.execute(
            """INSERT INTO insider_fetchers (source, last_fetched_at, records_fetched, errors)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_fetched_at = excluded.last_fetched_at,
                records_fetched = excluded.records_fetched,
                errors = excluded.errors""",
            (source, now, records, error),
        )
