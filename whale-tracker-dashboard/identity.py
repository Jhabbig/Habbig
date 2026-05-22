"""Manager / filer identity graph.

The `filer_profile` table aggregates everything we know about a CIK:
display name, kind (fund / insider / activist / congress), fund type,
primary person (when known), tags (e.g. "value", "activist", "tech").

Sources (composed automatically — no recurring API cost):
  - cik_ticker.json    → display_name, name normalisation
  - fund_filing        → display_name for funds, kind=fund
  - activist_stake     → display_name for activists, kind=activist
  - insider_txn        → display_name for insiders, kind=insider
  - activist_intent    → fund_type from LLM extraction (already pulled
                          when the LLM extractor runs)

Manual tags can be added via POST /api/admin/filer-tag (e.g. tag
0001067983 with ["value","long_term"] for Berkshire).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Iterable

import cik_ticker
import db

log = logging.getLogger("identity")


def _now() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def upsert_profile(cik: str, *, kind: str, display_name: str,
                   primary_person: str | None = None,
                   fund_type: str | None = None,
                   tags: list[str] | None = None,
                   source: str = "auto",
                   confidence: float = 0.9) -> None:
    if not cik:
        return
    with db.connect() as cx:
        cx.execute(
            """
            INSERT OR REPLACE INTO filer_profile
              (cik, kind, display_name, primary_person, fund_type, tags,
               source, confidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cik, kind, display_name, primary_person, fund_type,
             json.dumps(tags or []), source, float(confidence), _now()),
        )


def lookup(cik: str) -> dict | None:
    if not cik:
        return None
    with db.connect() as cx:
        row = cx.execute(
            "SELECT * FROM filer_profile WHERE cik = ?",
            (cik,),
        ).fetchone()
    if not row:
        return None
    r = dict(row)
    try:
        r["tags"] = json.loads(r.get("tags") or "[]")
    except json.JSONDecodeError:
        r["tags"] = []
    return r


def bulk_lookup(ciks: Iterable[str]) -> dict[str, dict]:
    ciks = [c for c in {*ciks} if c]
    if not ciks:
        return {}
    placeholders = ",".join(["?"] * len(ciks))
    with db.connect() as cx:
        rows = cx.execute(
            f"SELECT * FROM filer_profile WHERE cik IN ({placeholders})",
            ciks,
        ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        r = dict(row)
        try:
            r["tags"] = json.loads(r.get("tags") or "[]")
        except json.JSONDecodeError:
            r["tags"] = []
        out[r["cik"]] = r
    return out


async def rebuild_from_filings() -> dict[str, int]:
    """Compose filer_profile rows from existing tables. Idempotent."""
    await cik_ticker.ensure_loaded()
    counts = {"fund": 0, "activist": 0, "insider": 0}

    with db.connect() as cx:
        # Funds — display_name from fund_filing.fund_name (most recent wins).
        for r in cx.execute(
            """
            SELECT fund_cik AS cik, fund_name AS name,
                   MAX(filed_at)    AS latest_filed,
                   SUM(total_value) AS aum_proxy
            FROM fund_filing
            WHERE fund_cik IS NOT NULL AND fund_cik != ''
            GROUP BY fund_cik
            """
        ).fetchall():
            upsert_profile(
                r["cik"], kind="fund",
                display_name=r["name"] or cik_ticker.lookup_name(r["cik"]) or r["cik"],
                fund_type="institutional_manager",
                tags=[],
                source="auto",
            )
            counts["fund"] += 1

        # Activists — from activist_stake.filer_name.
        for r in cx.execute(
            """
            SELECT filer_cik AS cik, filer_name AS name, COUNT(*) AS n
            FROM activist_stake
            WHERE filer_cik IS NOT NULL AND filer_cik != ''
            GROUP BY filer_cik
            """
        ).fetchall():
            # If an LLM-extracted fund_type exists for this filer, prefer it.
            ft = cx.execute(
                """
                SELECT fund_type FROM activist_intent i
                JOIN activist_stake a ON a.accession = i.accession
                WHERE a.filer_cik = ? AND i.fund_type IS NOT NULL
                ORDER BY i.extracted_at DESC LIMIT 1
                """,
                (r["cik"],),
            ).fetchone()
            existing = cx.execute(
                "SELECT 1 FROM filer_profile WHERE cik = ? LIMIT 1",
                (r["cik"],),
            ).fetchone()
            # Don't downgrade fund profile back to activist if a fund row exists.
            if existing:
                continue
            upsert_profile(
                r["cik"], kind="activist",
                display_name=r["name"] or r["cik"],
                fund_type=(ft["fund_type"] if ft else None),
                tags=[],
                source="auto",
            )
            counts["activist"] += 1

        # Insiders — from insider_txn.reporter_name.
        for r in cx.execute(
            """
            SELECT reporter_cik AS cik, reporter_name AS name, COUNT(*) AS n
            FROM insider_txn
            WHERE reporter_cik IS NOT NULL AND reporter_cik != ''
            GROUP BY reporter_cik
            """
        ).fetchall():
            existing = cx.execute(
                "SELECT 1 FROM filer_profile WHERE cik = ? LIMIT 1",
                (r["cik"],),
            ).fetchone()
            if existing:
                continue
            upsert_profile(
                r["cik"], kind="insider",
                display_name=r["name"] or r["cik"],
                source="auto",
            )
            counts["insider"] += 1

    return counts


def tag(cik: str, tags: list[str]) -> dict | None:
    """Apply manual tags to an existing profile."""
    prof = lookup(cik)
    if not prof:
        return None
    existing = set(prof.get("tags") or [])
    existing.update(tags or [])
    upsert_profile(
        cik,
        kind=prof.get("kind") or "other",
        display_name=prof.get("display_name") or cik,
        primary_person=prof.get("primary_person"),
        fund_type=prof.get("fund_type"),
        tags=sorted(existing),
        source="manual",
        confidence=prof.get("confidence") or 0.9,
    )
    return lookup(cik)
