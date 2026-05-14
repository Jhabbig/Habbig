#!/usr/bin/env python3
"""Unit test for the EDGAR fetcher — no network.

Mocks ``httpx.Client.get`` with a recorded payload shape from
``data.sec.gov/submissions/CIK*.json`` and asserts ``fetch_recent_filings``
filters / shapes the result correctly. Also exercises the form-routing
``_insert_filing`` against an in-memory SQLite that mirrors the production
schema.

Run:
    python3 whale-dashboard/scripts/test_seed_13f.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make the scripts dir importable when invoked directly.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import seed_13f  # noqa: E402


# Minimal but realistic EDGAR submissions payload. The real API returns
# parallel arrays under filings.recent.
SAMPLE_PAYLOAD = {
    "cik": "1067983",
    "name": "BERKSHIRE HATHAWAY INC",
    "filings": {
        "recent": {
            "accessionNumber": [
                "0001067983-26-000007",  # 13F-HR ✓
                "0001140361-26-019283",  # 8-K — should be filtered out
                "0001067983-26-000003",  # SC 13D ✓
                "0001127602-26-014412",  # Form 4 ✓
                "0001127602-25-097710",  # 13F-HR/A amendment ✓
            ],
            "filingDate": [
                "2026-05-10",
                "2026-05-09",
                "2026-04-22",
                "2026-04-15",
                "2026-02-14",
            ],
            "reportDate": [
                "2026-03-31",
                "",
                "2026-04-20",
                "2026-04-15",
                "2025-12-31",
            ],
            "form": [
                "13F-HR",
                "8-K",
                "SC 13D",
                "4",
                "13F-HR/A",
            ],
        }
    },
}


def _mock_client_returning(payload: dict) -> MagicMock:
    """A MagicMock that mimics ``httpx.Client.get`` returning ``payload``."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value=payload)
    client = MagicMock()
    client.get = MagicMock(return_value=resp)
    return client


def test_fetch_recent_filings_filters_and_shapes() -> None:
    client = _mock_client_returning(SAMPLE_PAYLOAD)
    out = seed_13f.fetch_recent_filings(1067983, seed_13f.WANTED_FORMS, client=client)

    # 4 wanted forms, 1 ignored (8-K).
    assert len(out) == 4, f"expected 4 filings, got {len(out)}: {out}"

    forms = {f["form"] for f in out}
    assert forms == {"13F-HR", "SC 13D", "4", "13F-HR/A"}, forms

    # Spot-check shape on the first row.
    first = out[0]
    assert first["form"] == "13F-HR"
    assert first["accession"] == "0001067983-26-000007"
    assert first["date"] == "2026-05-10"
    assert first["report_date"] == "2026-03-31"
    assert first["cik"] == 1067983

    # The URL is the padded form, but request goes through the supplied client.
    call_url = client.get.call_args[0][0]
    assert "CIK0001067983.json" in call_url, call_url

    # The function must set a User-Agent header.
    headers = client.get.call_args[1].get("headers") or {}
    assert "User-Agent" in headers and headers["User-Agent"], "missing UA"

    print("PASS: fetch_recent_filings filters + shapes correctly")


def test_fetch_handles_empty_recent() -> None:
    """When EDGAR returns no recent filings, we get [] (not a crash)."""
    client = _mock_client_returning({"filings": {"recent": {}}})
    out = seed_13f.fetch_recent_filings(102909, seed_13f.WANTED_FORMS, client=client)
    assert out == [], out
    print("PASS: empty filings.recent returns []")


def test_insert_filing_routes_to_correct_table() -> None:
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    # Mini schema mirroring the parts of schema.sql we exercise.
    cur.executescript(
        """
        CREATE TABLE filings_13f (
            accession_no TEXT PRIMARY KEY, cik TEXT NOT NULL,
            period_of_report TEXT NOT NULL, filed_at TEXT NOT NULL,
            form_type TEXT NOT NULL DEFAULT '13F-HR',
            total_value_usd REAL, n_positions INTEGER NOT NULL DEFAULT 0,
            raw_url TEXT, created_at INTEGER NOT NULL
        );
        CREATE TABLE filings_13d (
            accession_no TEXT PRIMARY KEY, cik TEXT NOT NULL,
            subject_cik TEXT, subject_name TEXT NOT NULL, subject_ticker TEXT,
            form_type TEXT NOT NULL, event_date TEXT NOT NULL,
            filed_at TEXT NOT NULL, pct_held REAL, shares_held INTEGER,
            summary TEXT, is_activist INTEGER NOT NULL DEFAULT 0,
            raw_url TEXT, created_at INTEGER NOT NULL
        );
        CREATE TABLE filings_form4 (
            accession_no TEXT PRIMARY KEY, cik TEXT NOT NULL,
            reporter_name TEXT NOT NULL, reporter_title TEXT,
            issuer_cik TEXT, issuer_name TEXT NOT NULL, issuer_ticker TEXT,
            txn_date TEXT NOT NULL, txn_code TEXT,
            is_buy INTEGER NOT NULL DEFAULT 0,
            shares INTEGER, price_usd REAL, value_usd REAL,
            filed_at TEXT NOT NULL, raw_url TEXT, created_at INTEGER NOT NULL
        );
        """
    )

    base = {
        "cik": 1067983,
        "date": "2026-05-10",
        "report_date": "2026-03-31",
    }
    cik_padded = "0001067983"

    rows = [
        {**base, "form": "13F-HR",   "accession": "ACC-13F-001"},
        {**base, "form": "SC 13D",   "accession": "ACC-13D-001"},
        {**base, "form": "4",        "accession": "ACC-F4-001"},
        # Idempotency: same accession a second time → 0 rows inserted.
        {**base, "form": "13F-HR",   "accession": "ACC-13F-001"},
    ]
    counts = [seed_13f._insert_filing(cur, r, cik_padded) for r in rows]
    assert counts == [1, 1, 1, 0], counts

    # Each table has exactly one row.
    for t in ("filings_13f", "filings_13d", "filings_form4"):
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n == 1, f"{t}: expected 1 row, got {n}"

    # The view-empty helper should report False once any table has data.
    assert seed_13f.filings_view_is_empty(conn) is False
    print("PASS: _insert_filing routes by form + INSERT OR IGNORE works")


def test_real_cik_filter() -> None:
    """Synthetic ``X*`` CIKs must be rejected before any EDGAR call."""
    assert seed_13f._is_real_cik("0001067983") is True
    assert seed_13f._is_real_cik("1067983") is True
    assert seed_13f._is_real_cik(1067983) is True
    assert seed_13f._is_real_cik("XENGINENO1000") is False
    assert seed_13f._is_real_cik("") is False
    assert seed_13f._is_real_cik(None) is False
    print("PASS: _is_real_cik filter")


def test_user_agent_env() -> None:
    import os
    os.environ["EDGAR_USER_AGENT"] = "test-suite test@example.com"
    try:
        assert seed_13f._user_agent() == "test-suite test@example.com"
    finally:
        del os.environ["EDGAR_USER_AGENT"]
    # Default kicks in when env var is empty.
    assert seed_13f._user_agent() == seed_13f.DEFAULT_UA
    print("PASS: _user_agent env override + default")


def main() -> int:
    test_fetch_recent_filings_filters_and_shapes()
    test_fetch_handles_empty_recent()
    test_insert_filing_routes_to_correct_table()
    test_real_cik_filter()
    test_user_agent_env()
    print("\nALL OK — 5/5 tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
