#!/usr/bin/env python3
"""
Wallet identity layer.

Maps Polymarket proxy-wallet addresses to human-readable display names so
they show up in the unified insider feed alongside "Hon. Nancy Pelosi" and
"Cook Timothy D" instead of "0xabc123…".

Three sources, in priority order:
  1. Manual override     — set via `set_label(addr, name, source='manual')`
  2. Polymarket pseudonym — auto-imported from leaderboard rows
                            (wallet's own profile name)
  3. Off-chain research   — set with source='research', e.g. "Domer (theo4)"

Manual overrides always win. The auto-importer never overwrites a manual
label (we honour `source='manual'` rows as ground truth).

DB lives in `wallet_labels.db` next to insider_events.db.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "wallet_labels.db"

VALID_SOURCES = {"manual", "polymarket", "research", "auto"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_labels (
    address       TEXT PRIMARY KEY,        -- always lowercase
    display_name  TEXT NOT NULL,
    twitter       TEXT,
    source        TEXT NOT NULL,           -- one of VALID_SOURCES
    confidence    REAL NOT NULL DEFAULT 1.0,  -- 0–1, soft prior
    notes         TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_labels_source ON wallet_labels(source);
"""

_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def _norm(addr: str) -> str | None:
    if not addr or not isinstance(addr, str):
        return None
    a = addr.strip().lower()
    if not _ADDR_RE.match(a):
        return None
    return a


# ─── Writes ───────────────────────────────────────────────────────────

def set_label(
    address: str,
    display_name: str,
    *,
    source: str = "manual",
    twitter: str | None = None,
    confidence: float = 1.0,
    notes: str | None = None,
    overwrite: bool = True,
) -> bool:
    """
    Insert or update a label. Returns True if the row was written, False if
    skipped (e.g. tried to overwrite a manual label with overwrite=False).
    """
    addr = _norm(address)
    if not addr:
        raise ValueError(f"invalid address: {address!r}")
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid source: {source!r}")
    if not display_name or not display_name.strip():
        raise ValueError("display_name required")

    init_db()
    now = int(time.time())
    with _conn() as c:
        existing = c.execute(
            "SELECT source FROM wallet_labels WHERE address = ?", (addr,),
        ).fetchone()
        if existing:
            existing_source = existing["source"]
            if not overwrite:
                return False
            # Manual is sticky — auto/research/polymarket cannot overwrite manual
            if existing_source == "manual" and source != "manual":
                return False
            c.execute(
                """UPDATE wallet_labels
                   SET display_name=?, twitter=?, source=?, confidence=?,
                       notes=?, updated_at=?
                   WHERE address=?""",
                (display_name.strip(), twitter, source, confidence, notes, now, addr),
            )
        else:
            c.execute(
                """INSERT INTO wallet_labels
                   (address, display_name, twitter, source, confidence, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (addr, display_name.strip(), twitter, source, confidence, notes, now, now),
            )
    return True


def import_polymarket_pseudonyms(rows: Iterable[dict]) -> dict:
    """
    Bulk-import wallet→pseudonym pairs from Polymarket leaderboard data.
    Each input row should have at least {address, pseudonym|name}.
    Returns {imported, skipped_existing_manual, skipped_invalid}.
    """
    init_db()
    imported = skipped_manual = skipped_invalid = 0
    for row in rows:
        addr = _norm(row.get("address") or row.get("proxyWallet") or "")
        name = (row.get("pseudonym") or row.get("name") or "").strip()
        if not addr or not name:
            skipped_invalid += 1
            continue
        wrote = set_label(addr, name, source="polymarket", confidence=0.7,
                          overwrite=True)
        if wrote:
            imported += 1
        else:
            skipped_manual += 1
    return {
        "imported": imported,
        "skipped_existing_manual": skipped_manual,
        "skipped_invalid": skipped_invalid,
    }


def delete_label(address: str) -> bool:
    addr = _norm(address)
    if not addr:
        return False
    init_db()
    with _conn() as c:
        cur = c.execute("DELETE FROM wallet_labels WHERE address = ?", (addr,))
        return cur.rowcount > 0


# ─── Reads ────────────────────────────────────────────────────────────

def get_label(address: str) -> dict | None:
    addr = _norm(address)
    if not addr:
        return None
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM wallet_labels WHERE address = ?", (addr,),
        ).fetchone()
    return dict(row) if row else None


def get_display_name(address: str, fallback: str | None = None) -> str:
    """Quick helper for the bridge: returns name or 0xabc123… fallback."""
    label = get_label(address)
    if label:
        return label["display_name"]
    if fallback:
        return fallback
    addr = _norm(address)
    return (addr[:6] + "…" + addr[-4:]) if addr else "unknown"


def bulk_get(addresses: Iterable[str]) -> dict[str, dict]:
    """Single-query lookup for many addresses; returns {address: label_row}."""
    addrs = [_norm(a) for a in addresses if a]
    addrs = [a for a in addrs if a]
    if not addrs:
        return {}
    init_db()
    out: dict[str, dict] = {}
    # SQLite caps parameter count around 999, batch defensively
    BATCH = 500
    with _conn() as c:
        for i in range(0, len(addrs), BATCH):
            chunk = addrs[i:i + BATCH]
            placeholders = ",".join("?" for _ in chunk)
            rows = c.execute(
                f"SELECT * FROM wallet_labels WHERE address IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                out[r["address"]] = dict(r)
    return out


def list_labels(*, source: str | None = None, limit: int = 500) -> list[dict]:
    init_db()
    sql = "SELECT * FROM wallet_labels"
    params: list = []
    if source:
        if source not in VALID_SOURCES:
            raise ValueError(f"invalid source: {source!r}")
        sql += " WHERE source = ?"
        params.append(source)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def stats_summary() -> dict:
    init_db()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM wallet_labels").fetchone()["n"]
        by_source = {
            r["source"]: r["n"]
            for r in c.execute(
                "SELECT source, COUNT(*) AS n FROM wallet_labels GROUP BY source"
            ).fetchall()
        }
    return {"total_labels": total, "by_source": by_source}


if __name__ == "__main__":
    import json
    init_db()
    print(json.dumps(stats_summary(), indent=2))
