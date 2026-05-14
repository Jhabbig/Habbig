#!/usr/bin/env python3
"""
seed_13f.py — SEC EDGAR ingestion for Whale Watch.

Pulls recent filings for each whale CIK in ``data/whales.yaml`` and
upserts them into ``whale.sqlite``. Three filing families land in three
different tables (``filings_13f``, ``filings_13d``, ``filings_form4``);
``filings_unified`` is a VIEW that unions them for the Live Feed.

This module is import-safe — ``main()`` runs the full sweep, and
``fetch_recent_filings()`` is exposed for unit tests and the server's
startup hook.

EDGAR usage (per https://www.sec.gov/os/accessing-edgar-data):
  * A descriptive ``User-Agent`` header is mandatory; missing/empty
    UA -> 403. Set ``EDGAR_USER_AGENT`` env var or accept the default.
  * Global rate limit ~10 req/s — we sleep ``EDGAR_RATE_DELAY`` (default
    0.15s) between calls and back off on 429. One CIK = one request,
    so the full sweep is a few seconds.
  * ``data.sec.gov/submissions/CIK<10-digit>.json`` returns recent
    filings inline; older history is in paginated ``files/*.json``
    siblings. We only consume ``filings.recent`` here — enough for the
    Live Feed. Deeper backfill is left to a follow-up script.

The 13F holdings table (informationtable.xml) and Form 4 transaction
detail are intentionally NOT fetched here. This seeder populates the
filing-header rows so the Live Feed renders real EDGAR data; per-filing
detail fetches can layer on later without re-designing this entrypoint.

Manual run:
    EDGAR_USER_AGENT="narve.ai sho@cakarel.com" python3 scripts/seed_13f.py
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
WHALES_YAML = ROOT / "data" / "whales.yaml"
DB_PATH = Path(os.environ.get("WHALE_DB_PATH", str(ROOT / "whale.sqlite")))

BASE = "https://data.sec.gov"
DEFAULT_UA = "narve.ai sho@cakarel.com"

# EDGAR's documented limit is ~10 req/s. 0.15s ~ 6.6 req/s — well inside.
RATE_DELAY = float(os.environ.get("EDGAR_RATE_DELAY", "0.15"))
HTTP_TIMEOUT = float(os.environ.get("EDGAR_HTTP_TIMEOUT", "20.0"))

# Forms we care about. EDGAR returns form strings verbatim from the filing
# cover, so we match the canonical names. ``13F-HR/A`` and ``SC 13D/A`` are
# amendments — we want them too.
FORMS_13F = {"13F-HR", "13F-HR/A"}
FORMS_13D = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}
FORMS_FORM4 = {"4", "4/A"}
WANTED_FORMS: frozenset[str] = frozenset(FORMS_13F | FORMS_13D | FORMS_FORM4)

log = logging.getLogger("whale.seed_13f")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _user_agent() -> str:
    """Resolve the EDGAR User-Agent. Env > default. Never empty."""
    ua = (os.environ.get("EDGAR_USER_AGENT") or "").strip()
    return ua or DEFAULT_UA


def _is_real_cik(cik: str | int | None) -> bool:
    """server.py synthesises ``X<NAME>`` CIKs for whales without a verified
    EDGAR number. Those rows must be skipped here — EDGAR would 404."""
    if cik is None:
        return False
    s = str(cik).strip()
    return bool(s) and s.isdigit()


def _accession_to_url(cik: int, accession: str) -> str:
    """Filing-index URL on edgar.sec.gov (humans visit this; we store it as
    ``raw_url``)."""
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/"


# ──────────────────────────────────────────────────────────────────────────────
# Fetcher — kept tiny + pure so tests can mock httpx.get
# ──────────────────────────────────────────────────────────────────────────────

def fetch_recent_filings(
    cik: int | str,
    form_types: Iterable[str] = WANTED_FORMS,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Fetch the ``filings.recent`` slice for ``cik`` and return a list of
    {form, date, accession, cik} dicts filtered to ``form_types``.

    Raises ``httpx.HTTPStatusError`` on non-2xx. Caller (``main``) catches
    these and continues; this function stays pure for testability.
    """
    cik_int = int(cik)
    url = f"{BASE}/submissions/CIK{cik_int:010d}.json"
    headers = {"User-Agent": _user_agent(), "Accept-Encoding": "gzip"}

    if client is None:
        r = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    else:
        r = client.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    report_dates = recent.get("reportDate") or [""] * len(forms)

    wanted = set(form_types)
    out: list[dict[str, Any]] = []
    for form, fdate, acc, rdate in zip(forms, dates, accs, report_dates):
        if form in wanted:
            out.append(
                {
                    "form": form,
                    "date": fdate,
                    "report_date": rdate or fdate,
                    "accession": acc,
                    "cik": cik_int,
                }
            )
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Persistence — the schema splits forms across three tables.
# ──────────────────────────────────────────────────────────────────────────────

def _insert_filing(c: sqlite3.Cursor, f: dict[str, Any], cik_padded: str) -> int:
    """Route one filing dict to the right table. Returns 1 if a row was
    inserted, 0 if already present. Idempotent via INSERT OR IGNORE."""
    form = f["form"]
    accession = f["accession"]
    filing_date = f["date"]
    report_date = f.get("report_date") or filing_date
    raw_url = _accession_to_url(f["cik"], accession)
    now = int(time.time())

    if form in FORMS_13F:
        c.execute(
            """
            INSERT OR IGNORE INTO filings_13f
                (accession_no, cik, period_of_report, filed_at, form_type,
                 total_value_usd, n_positions, raw_url, created_at)
            VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?)
            """,
            (accession, cik_padded, report_date, filing_date, form, raw_url, now),
        )
        return c.rowcount or 0

    if form in FORMS_13D:
        c.execute(
            """
            INSERT OR IGNORE INTO filings_13d
                (accession_no, cik, subject_cik, subject_name, subject_ticker,
                 form_type, event_date, filed_at, pct_held, shares_held,
                 summary, is_activist, raw_url, created_at)
            VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?)
            """,
            (
                accession,
                cik_padded,
                "(pending detail fetch)",
                form,
                report_date,
                filing_date,
                raw_url,
                now,
            ),
        )
        return c.rowcount or 0

    if form in FORMS_FORM4:
        c.execute(
            """
            INSERT OR IGNORE INTO filings_form4
                (accession_no, cik, reporter_name, reporter_title,
                 issuer_cik, issuer_name, issuer_ticker, txn_date, txn_code,
                 is_buy, shares, price_usd, value_usd, filed_at, raw_url, created_at)
            VALUES (?, ?, ?, NULL, NULL, ?, NULL, ?, NULL, 0, NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                accession,
                cik_padded,
                "(pending detail fetch)",
                "(pending detail fetch)",
                report_date,
                filing_date,
                raw_url,
                now,
            ),
        )
        return c.rowcount or 0

    return 0


def _load_whales() -> list[dict[str, Any]]:
    if not WHALES_YAML.exists():
        log.error("whales.yaml missing at %s", WHALES_YAML)
        return []
    doc = yaml.safe_load(WHALES_YAML.read_text(encoding="utf-8")) or {}
    return list(doc.get("whales") or [])


def _is_table_empty(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        return row is None
    except sqlite3.Error:
        return True


def filings_view_is_empty(conn: sqlite3.Connection) -> bool:
    """The view is a UNION of three tables — empty when all three are empty."""
    return (
        _is_table_empty(conn, "filings_13f")
        and _is_table_empty(conn, "filings_13d")
        and _is_table_empty(conn, "filings_form4")
    )


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────

def main(db_path: Path | str | None = None) -> int:
    """Sweep every real-CIK whale once. Returns count of rows inserted.

    Failures on a single CIK are logged and the sweep continues.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    whales = _load_whales()
    if not whales:
        log.warning("no whales loaded; nothing to seed")
        return 0

    db = Path(db_path) if db_path else DB_PATH
    if not db.exists():
        log.error("DB not found at %s — start the server once to apply schema", db)
        return 0

    conn = sqlite3.connect(str(db), timeout=10.0)
    cursor = conn.cursor()

    headers = {"User-Agent": _user_agent(), "Accept-Encoding": "gzip"}
    inserted = 0
    skipped_no_cik = 0
    errors = 0

    with httpx.Client(headers=headers, timeout=HTTP_TIMEOUT) as client:
        for w in whales:
            raw_cik = w.get("cik")
            name = (w.get("name") or "?").strip()
            if not _is_real_cik(raw_cik):
                skipped_no_cik += 1
                continue

            cik_int = int(raw_cik)
            cik_padded = f"{cik_int:010d}"

            try:
                filings = fetch_recent_filings(cik_int, WANTED_FORMS, client=client)
            except httpx.HTTPStatusError as e:
                # 404 is expected when the YAML carries a stale or wrong CIK —
                # don't fail the sweep, just log and move on.
                status = e.response.status_code if e.response is not None else "?"
                log.warning("EDGAR %s for %s (CIK %s) — skipping", status, name, cik_padded)
                errors += 1
                time.sleep(RATE_DELAY)
                continue
            except (httpx.RequestError, ValueError) as e:
                log.warning("EDGAR fetch failed for %s (CIK %s): %s", name, cik_padded, e)
                errors += 1
                time.sleep(RATE_DELAY)
                continue

            for f in filings:
                try:
                    inserted += _insert_filing(cursor, f, cik_padded)
                except sqlite3.Error as e:
                    log.warning(
                        "insert failed for %s/%s (%s): %s",
                        name, f.get("accession"), f.get("form"), e,
                    )

            conn.commit()
            time.sleep(RATE_DELAY)

    conn.close()
    log.info(
        "EDGAR sweep done — inserted=%d, skipped_no_cik=%d, errors=%d",
        inserted, skipped_no_cik, errors,
    )
    return inserted


if __name__ == "__main__":
    sys.exit(0 if main() >= 0 else 1)
