#!/usr/bin/env python3
"""
Congressional STOCK Act PTR ingester — Pelosi, Tuberville, et al.

Two-tier source strategy (per chamber):

  1. Primary: Stock Watcher S3 dumps (rich — includes parsed ticker, side, amount)
     - https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json
     - https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json
     These have started returning 403 in 2026 — community projects, no SLA.

  2. Fallback (House only): the official House Clerk XML index at
     https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{Year}FD.zip
     This is the canonical source and won't get blocked. Trade-off: it's a
     *filing-level* index (Member name, doc id, filing date, type='P' for PTR),
     not a transaction-level dump. We still surface every PTR filing as an
     event so the watchlist + unified feed work; ticker/side/amount stay null
     (those are inside the PDF and need OCR to extract — out of scope here).

  Senate has no comparable bulk index (efdsearch.senate.gov is JS+CAPTCHA),
  so when its Stock Watcher dump fails we just emit no Senate events.

We re-fetch every 6h because the sources refresh daily; dedup via
UNIQUE(venue, source_id) makes re-ingestion a no-op.

Latency reality check: Congressional disclosures are required within 45 days
of the trade, so this is a *narrative* feed, not an alpha feed. The cross-venue
correlation engine (correlation.py) skips events without a symbol, so the
fallback rows don't pollute the |Δ_pre| feed — they only show up in the
unified insiders tab and in alerts for watched actors.
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from typing import Iterable

import httpx

import insider_events

logger = logging.getLogger(__name__)

HOUSE_URL = (
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/data/all_transactions.json"
)
SENATE_URL = (
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/aggregate/all_transactions.json"
)
# Official fallback when the Stock Watcher S3 dump 403s. We pull the current
# year's index plus optionally the previous year for cross-year backfill.
HOUSE_CLERK_INDEX_URL = (
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
)
HOUSE_CLERK_PTR_PDF = (
    "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
)
HTTP_TIMEOUT = 60.0  # the dumps can be ~30 MB
USER_AGENT = "narve-insider-tracker (research; contact via narve.ai)"


# ─── Fetch ────────────────────────────────────────────────────────────

def _fetch_json(url: str) -> list[dict] | None:
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, list):
            logger.warning("PTR feed %s did not return a list", url)
            return None
        return data
    except Exception as e:
        logger.warning("PTR fetch failed for %s: %s", url, e)
        return None


# ─── Parse helpers ────────────────────────────────────────────────────

# House's `type`: 'purchase' | 'sale_full' | 'sale_partial' | 'exchange'
# Senate's `type`: 'Purchase' | 'Sale (Full)' | 'Sale (Partial)' | 'Exchange'
_TYPE_TO_SIDE = {
    "purchase": "buy",
    "sale_full": "sell",
    "sale_partial": "sell",
    "sale": "sell",
    "exchange": "exchange",
}


def _normalize_type(raw: str | None) -> str:
    if not raw:
        return "other"
    t = raw.strip().lower()
    t = t.replace(" (full)", "_full").replace(" (partial)", "_partial")
    t = t.replace(" ", "_")
    return _TYPE_TO_SIDE.get(t, "other")


# Amount strings vary across sources. Cover the documented buckets and a
# few free-form variants. Returns (low, high) USD or (None, None).
_AMOUNT_RE = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)(?:\s*-\s*\$?\s*([\d,]+(?:\.\d+)?))?",
)
_OVER_RE = re.compile(r"(?:over|>|\+)\s*\$?\s*([\d,]+)", re.IGNORECASE)


def parse_amount(amount: str | None) -> tuple[float | None, float | None]:
    if not amount:
        return None, None
    s = amount.strip()
    if not s:
        return None, None

    # "Over $50,000,000" / "$50,000,000+" forms
    over = _OVER_RE.search(s)
    if over and ("over" in s.lower() or "+" in s or ">" in s):
        try:
            low = float(over.group(1).replace(",", ""))
            return low, None
        except ValueError:
            pass

    m = _AMOUNT_RE.search(s)
    if not m:
        return None, None
    try:
        low = float(m.group(1).replace(",", ""))
    except ValueError:
        return None, None
    high: float | None = None
    if m.group(2):
        try:
            high = float(m.group(2).replace(",", ""))
        except ValueError:
            high = None
    return low, high


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y")


def _parse_date(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip()
    # Reject obvious garbage upstream sometimes emits ('--', '0000-00-00', etc.)
    if not s or s in ("--", "0000-00-00"):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def _clean_ticker(t: str | None) -> str | None:
    """House feed sometimes has '--' for tickers it couldn't extract."""
    if not t:
        return None
    t = t.strip().upper()
    if not t or t in ("--", "N/A", "NONE", "0"):
        return None
    # Only allow standard ticker-shaped things; drop weird CUSIP-like stuff
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t):
        return None
    return t


def _actor_id_from_name(name: str, chamber: str) -> str:
    """Stable id like 'house:pelosi-nancy' or 'senate:tuberville-tommy'."""
    n = re.sub(r"^(hon\.?|mr\.?|mrs\.?|ms\.?|dr\.?|sen\.?|rep\.?)\s+", "",
               name.strip(), flags=re.IGNORECASE)
    n = re.sub(r"[^a-z0-9\s]", "", n.lower())
    parts = n.split()
    if len(parts) >= 2:
        slug = f"{parts[-1]}-{parts[0]}"
    else:
        slug = parts[0] if parts else "unknown"
    return f"{chamber}:{slug}"


# ─── House → events ──────────────────────────────────────────────────

def _row_house(tx: dict, idx: int) -> dict | None:
    rep_name = (tx.get("representative") or "").strip()
    if not rep_name:
        return None
    ticker = _clean_ticker(tx.get("ticker"))
    side = _normalize_type(tx.get("type"))
    low, high = parse_amount(tx.get("amount"))
    ts_executed = _parse_date(tx.get("transaction_date"))
    ts_filed = _parse_date(tx.get("disclosure_date"))
    ptr_link = (tx.get("ptr_link") or "").strip() or None

    # Source id: ptr_link is the official document; multiple txs per PTR get
    # disambiguated by their index within the doc.
    source_id_base = ptr_link or f"house:{rep_name}:{tx.get('disclosure_date','')}"
    source_id = f"{source_id_base}#{idx}"

    actor_id = _actor_id_from_name(rep_name, "house")
    actor_role = "Representative"
    district = (tx.get("district") or "").strip()
    if district:
        actor_role = f"Representative ({district})"

    return {
        "venue": "congress_ptr",
        "source_id": source_id,
        "ts_filed": ts_filed,
        "ts_executed": ts_executed,
        "actor_id": actor_id,
        "actor_label": rep_name,
        "actor_role": actor_role,
        "symbol": ticker,
        "symbol_name": (tx.get("asset_description") or "").strip() or None,
        "side": side,
        "shares": None,
        "price": None,
        "size_usd_low": low,
        "size_usd_high": high,
        "raw_url": ptr_link,
        "extra": {
            "chamber": "house",
            "owner": tx.get("owner"),
            "asset_description": tx.get("asset_description"),
            "raw_type": tx.get("type"),
            "raw_amount": tx.get("amount"),
            "cap_gains_over_200": tx.get("cap_gains_over_200_usd"),
        },
    }


# ─── House Clerk XML fallback ────────────────────────────────────────

def _fetch_house_clerk_xml(year: int) -> list[dict] | None:
    """
    Pull the official House Clerk financial-disclosure index for a given year.
    Returns a list of {first, last, prefix, doc_id, filing_date, filing_type,
    state_dst, year} dicts — one per Member filing — or None on failure.
    """
    url = HOUSE_CLERK_INDEX_URL.format(year=year)
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            r = client.get(url)
            if r.status_code != 200:
                logger.warning("House Clerk XML fetch %s: HTTP %d", year, r.status_code)
                return None
            zbytes = r.content
        # The zip contains {year}FD.xml + {year}FD.txt; we only need the XML.
        with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
            xml_name = f"{year}FD.xml"
            if xml_name not in zf.namelist():
                # Sometimes the file is uppercased differently — be tolerant.
                xml_candidates = [n for n in zf.namelist() if n.lower().endswith(".xml")]
                if not xml_candidates:
                    return None
                xml_name = xml_candidates[0]
            with zf.open(xml_name) as f:
                tree = ET.parse(f)
        root = tree.getroot()
    except Exception as e:
        logger.warning("House Clerk XML parse failed for %s: %s", year, e)
        return None

    out: list[dict] = []
    for member in root.findall(".//Member"):
        def _txt(tag: str) -> str:
            el = member.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        out.append({
            "prefix":       _txt("Prefix"),
            "first":        _txt("First"),
            "last":         _txt("Last"),
            "suffix":       _txt("Suffix"),
            "filing_type":  _txt("FilingType"),
            "state_dst":    _txt("StateDst"),
            "year":         _txt("Year") or str(year),
            "filing_date":  _txt("FilingDate"),
            "doc_id":       _txt("DocID"),
        })
    return out


def _row_house_clerk(rec: dict, idx: int) -> dict | None:
    """One filing → one event. PTRs only (FilingType='P')."""
    if (rec.get("filing_type") or "").upper() != "P":
        return None  # not a periodic transaction report
    first = rec.get("first") or ""
    last = rec.get("last") or ""
    if not last:
        return None
    full = f"{rec.get('prefix','')} {first} {last} {rec.get('suffix','')}".strip()
    full = re.sub(r"\s+", " ", full)
    actor_id = _actor_id_from_name(f"{first} {last}", "house")
    actor_role = (
        f"Representative ({rec['state_dst']})"
        if rec.get("state_dst") else "Representative"
    )
    ts_filed = _parse_date(rec.get("filing_date"))
    year = rec.get("year") or ""
    doc_id = rec.get("doc_id") or ""
    pdf_url = (
        HOUSE_CLERK_PTR_PDF.format(year=year, doc_id=doc_id)
        if year and doc_id else None
    )
    # Stable across re-runs: the Clerk's DocID uniquely identifies the filing.
    source_id = f"house-clerk:{year}:{doc_id}" if doc_id else f"house-clerk:{idx}"

    return {
        "venue": "congress_ptr",
        "source_id": source_id,
        "ts_filed": ts_filed,
        # No transaction-level execution date in the index — only the filing date.
        "ts_executed": None,
        "actor_id": actor_id,
        "actor_label": full or last,
        "actor_role": actor_role,
        # No symbol at the index level — the actual ticker(s) are inside the
        # PDF. correlation.py skips symbol-less events, which is the right
        # behaviour: they show up in the unified feed and watchlist alerts,
        # but not in the cross-venue Δ_pre rankings (where they'd be noise).
        "symbol": None,
        "symbol_name": "PTR (filing only — see linked PDF for transactions)",
        "side": "other",
        "shares": None,
        "price": None,
        "size_usd_low": None,
        "size_usd_high": None,
        "raw_url": pdf_url,
        "extra": {
            "chamber": "house",
            "source": "house-clerk-xml",
            "doc_id": doc_id,
            "year": year,
            "state_dst": rec.get("state_dst"),
            "filing_type": rec.get("filing_type"),
            "raw_filing_date": rec.get("filing_date"),
        },
    }


# ─── Senate → events ─────────────────────────────────────────────────

def _row_senate(tx: dict, idx: int) -> dict | None:
    sen_name = (tx.get("senator") or "").strip()
    if not sen_name:
        return None
    ticker = _clean_ticker(tx.get("ticker"))
    side = _normalize_type(tx.get("type"))
    low, high = parse_amount(tx.get("amount"))
    ts_executed = _parse_date(tx.get("transaction_date"))
    # Senate dumps don't always carry a separate disclosure date; fall back to
    # transaction_date so the row is still time-orderable.
    ts_filed = _parse_date(tx.get("disclosure_date") or tx.get("transaction_date"))
    ptr_link = (tx.get("ptr_link") or "").strip() or None

    source_id_base = ptr_link or f"senate:{sen_name}:{tx.get('transaction_date','')}"
    source_id = f"{source_id_base}#{idx}"

    actor_id = _actor_id_from_name(sen_name, "senate")
    return {
        "venue": "congress_ptr",
        "source_id": source_id,
        "ts_filed": ts_filed,
        "ts_executed": ts_executed,
        "actor_id": actor_id,
        "actor_label": sen_name,
        "actor_role": "Senator",
        "symbol": ticker,
        "symbol_name": (tx.get("asset_description") or "").strip() or None,
        "side": side,
        "shares": None,
        "price": None,
        "size_usd_low": low,
        "size_usd_high": high,
        "raw_url": ptr_link,
        "extra": {
            "chamber": "senate",
            "owner": tx.get("owner"),
            "asset_description": tx.get("asset_description"),
            "asset_type": tx.get("asset_type"),
            "raw_type": tx.get("type"),
            "raw_amount": tx.get("amount"),
            "comment": tx.get("comment"),
        },
    }


# ─── Top-level run ───────────────────────────────────────────────────

def _to_rows(feed: list[dict], builder) -> Iterable[dict]:
    for i, tx in enumerate(feed):
        try:
            row = builder(tx, i)
            if row:
                yield row
        except Exception as e:
            logger.warning("PTR row build failed (idx=%d): %s", i, e)


def run_ingest(
    *,
    house: bool = True,
    senate: bool = True,
    only_since_filed_days: int | None = None,
) -> dict:
    """
    Pull both PTR dumps and land them into insider_events.

    only_since_filed_days: if set, drop rows whose ts_filed is older than that
    cutoff before upserting. Useful in production to keep the hot table small;
    leave None for a full historical backfill.
    """
    insider_events.init_db()
    summary: dict = {"house": None, "senate": None}
    cutoff_ts = None
    if only_since_filed_days:
        cutoff_ts = int(datetime.now(timezone.utc).timestamp() - only_since_filed_days * 86400)

    def _filter(rows: Iterable[dict]) -> Iterable[dict]:
        if cutoff_ts is None:
            yield from rows
            return
        for r in rows:
            tsf = r.get("ts_filed") or r.get("ts_executed")
            if tsf is None or tsf >= cutoff_ts:
                yield r

    if house:
        feed = _fetch_json(HOUSE_URL)
        if feed is not None:
            res = insider_events.upsert_many(_filter(_to_rows(feed, _row_house)))
            summary["house"] = {
                "ok": True, "source": "stock-watcher",
                "rows_seen": len(feed), **res,
            }
        else:
            # Stock Watcher dump unavailable — fall back to House Clerk XML.
            # We pull the current year (and previous year if cutoff covers it)
            # so the unified feed at least surfaces "Member X filed PTR Y on Z".
            now = datetime.now(timezone.utc)
            years_to_fetch = [now.year]
            if cutoff_ts is not None:
                cutoff_dt = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc)
                if cutoff_dt.year < now.year:
                    years_to_fetch.append(now.year - 1)
            elif only_since_filed_days is None:
                # Full backfill mode — pull the previous year too for context.
                years_to_fetch.append(now.year - 1)

            all_records: list[dict] = []
            year_results: dict = {}
            for y in years_to_fetch:
                recs = _fetch_house_clerk_xml(y)
                if recs is None:
                    year_results[str(y)] = {"ok": False, "reason": "fetch_failed"}
                else:
                    year_results[str(y)] = {"ok": True, "rows_seen": len(recs)}
                    all_records.extend(recs)

            if not all_records:
                summary["house"] = {
                    "ok": False, "source": "house-clerk-xml-fallback",
                    "reason": "stock-watcher 403 + clerk fallback empty",
                    "year_results": year_results,
                }
            else:
                res = insider_events.upsert_many(
                    _filter(_to_rows(all_records, _row_house_clerk)),
                )
                summary["house"] = {
                    "ok": True, "source": "house-clerk-xml-fallback",
                    "rows_seen": len(all_records),
                    "year_results": year_results,
                    **res,
                }

    if senate:
        feed = _fetch_json(SENATE_URL)
        if feed is None:
            # No comparable bulk source exists for the Senate (efdsearch.senate.gov
            # is JS+CAPTCHA). Surface the failure but don't pretend to have a
            # fallback — the rest of the pipeline still works on House data.
            summary["senate"] = {
                "ok": False, "source": "stock-watcher",
                "reason": "fetch_failed (no senate fallback available)",
            }
        else:
            res = insider_events.upsert_many(_filter(_to_rows(feed, _row_senate)))
            summary["senate"] = {
                "ok": True, "source": "stock-watcher",
                "rows_seen": len(feed), **res,
            }

    return summary


# ─── PDF enrichment ──────────────────────────────────────────────────
#
# House Clerk XML gives us filing-level rows (no ticker / side / amount).
# This pass walks those rows, fetches the linked PDF, parses it via
# ptr_pdf_parser, and replaces each filing row with N transaction-level
# detail rows. The detail rows carry real symbols, so they show up in the
# cross-venue correlation engine just like Form 4 events.
#
# Idempotent: detail rows have composite source_ids
# 'house-clerk-detail:{year}:{doc_id}#{idx}', so re-running is a no-op.
# Filing rows are only replaced after a successful parse — failed parses
# leave the filing row in place as a fallback.

import sqlite3
import time
from pathlib import Path

# Lazy import so insider_events stays the only hard dep at module load —
# ptr_pdf_parser shells out to pdftotext which may not be installed
# everywhere (parser is_available() handles that gracefully).
def _enrich_imports():
    try:
        import ptr_pdf_parser
        return ptr_pdf_parser
    except ImportError:
        return None


# Side mapping mirrors what the parser produces (P→buy, S/S(partial)→sell, E→exchange).
def _detail_row_from_parse(filing: dict, tx: dict, idx: int) -> dict | None:
    ticker = (tx.get("ticker") or "").strip().upper() or None
    if not ticker and not (tx.get("asset_name") or "").strip():
        return None
    extra_filing = filing.get("extra") or {}
    year = extra_filing.get("year") or ""
    doc_id = extra_filing.get("doc_id") or ""
    source_id = (
        f"house-clerk-detail:{year}:{doc_id}#{idx}"
        if doc_id else f"house-clerk-detail:{filing.get('id')}#{idx}"
    )
    side = tx.get("side") or "other"
    asset_type = tx.get("asset_type")
    # Options on a stock count as a derivative-style position; flag in extra
    # but keep side as buy/sell so the correlation engine still picks them up.
    if asset_type and asset_type.upper() in ("OP", "WAR"):
        # Don't overwrite side; just annotate.
        pass

    return {
        "venue": "congress_ptr",
        "source_id": source_id,
        "ts_filed": filing.get("ts_filed"),
        "ts_executed": tx.get("tx_date_ts") or filing.get("ts_filed"),
        "actor_id": filing.get("actor_id"),
        "actor_label": filing.get("actor_label"),
        "actor_role": filing.get("actor_role"),
        "symbol": ticker,
        "symbol_name": (tx.get("asset_name") or "")[:120] or None,
        "side": side,
        "shares": None,
        "price": None,
        "size_usd_low": tx.get("amount_low"),
        "size_usd_high": tx.get("amount_high") or tx.get("amount_over"),
        "raw_url": filing.get("raw_url"),
        "extra": {
            "chamber": "house",
            "source": "house-clerk-pdf",
            "doc_id": doc_id,
            "year": year,
            "owner": tx.get("owner"),
            "asset_type": asset_type,
            "raw_tx_type": tx.get("tx_type_raw"),
            "raw_tx_date": tx.get("tx_date_raw"),
            "raw_notif_date": tx.get("notif_date_raw"),
            "amount_over_floor": tx.get("amount_over"),
            "description": tx.get("description"),
        },
    }


def _filing_rows_to_enrich(limit: int) -> list[dict]:
    """
    SQL: filing-level rows that haven't been enriched yet.

    Uses an extra_json JSON-LIKE filter so a previously-enriched row stays
    out of the queue even after the 6h PTR re-ingest re-asserts it. (We
    can't rely on row deletion — the next PTR poll would re-insert the
    same source_id immediately, putting us in a fetch+parse loop.)
    """
    insider_events.init_db()
    db = Path(__file__).parent / "insider_events.db"
    rows: list[dict] = []
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(
            """
            SELECT * FROM insider_events
            WHERE venue = 'congress_ptr'
              AND symbol IS NULL
              AND source_id LIKE 'house-clerk:%'
              AND raw_url IS NOT NULL
              AND (extra_json IS NULL
                   OR extra_json NOT LIKE '%"enriched": true%')
            ORDER BY COALESCE(ts_filed, created_at) DESC
            LIMIT ?
            """,
            (limit,),
        )
        for r in cur.fetchall():
            d = dict(r)
            # Re-hydrate the extra dict; insider_events stores it as JSON.
            import json as _json
            try:
                d["extra"] = _json.loads(d.get("extra_json") or "{}") or {}
            except Exception:
                d["extra"] = {}
            rows.append(d)
    return rows


def _mark_enriched(filing_id: int, tx_count: int) -> None:
    """
    Update the parent filing row's extra_json to record that we've already
    expanded it. Re-ingestion is still idempotent (UNIQUE source_id) but
    enrichment will skip this row on future passes.
    """
    import json as _json
    db = Path(__file__).parent / "insider_events.db"
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT extra_json FROM insider_events WHERE id = ?", (filing_id,),
        ).fetchone()
        if not row:
            return
        try:
            extra = _json.loads(row["extra_json"] or "{}") or {}
        except Exception:
            extra = {}
        extra["enriched"] = True
        extra["enriched_tx_count"] = tx_count
        extra["enriched_at"] = int(time.time())
        c.execute(
            "UPDATE insider_events SET extra_json = ? WHERE id = ?",
            (_json.dumps(extra, default=str), filing_id),
        )
        c.commit()


def enrich_house_filings(
    *,
    max_filings: int = 30,
    pause_between: float = 0.4,
) -> dict:
    """
    Fetch + parse PDFs for unenriched House Clerk filings, replace each
    parent filing row with N transaction-level detail rows.

    Returns {filings_seen, parsed, txs_written, parse_failures, fetch_failures}.
    Cap is intentionally low (30/pass) to avoid hammering the Clerk; the
    poller calls this every 30 minutes so the queue drains over time.
    """
    parser = _enrich_imports()
    if parser is None:
        return {"ok": False, "reason": "ptr_pdf_parser unavailable"}
    if not parser.is_available():
        return {"ok": False, "reason": "pdftotext not installed (apt: poppler-utils)"}

    filings = _filing_rows_to_enrich(max_filings)
    if not filings:
        return {
            "ok": True, "filings_seen": 0, "parsed": 0,
            "txs_written": 0, "parse_failures": 0, "fetch_failures": 0,
        }

    parsed_count = txs_written = parse_failures = fetch_failures = 0
    for f in filings:
        try:
            txs = parser.parse_ptr_pdf(f["raw_url"])
        except Exception as e:
            logger.debug("PTR PDF parse threw for %s: %s", f.get("raw_url"), e)
            txs = []
            fetch_failures += 1

        if not txs:
            parse_failures += 1
            time.sleep(pause_between)
            continue
        parsed_count += 1

        rows = []
        for i, t in enumerate(txs):
            row = _detail_row_from_parse(f, t, i)
            if row:
                rows.append(row)
        if not rows:
            parse_failures += 1
            time.sleep(pause_between)
            continue

        res = insider_events.upsert_many(rows)
        txs_written += res.get("inserted", 0)
        # Mark the parent enriched so the next pass skips it. We deliberately
        # don't delete — the unified-feed UI filters by `symbol IS NOT NULL`
        # for the primary view, and the parent row is still useful as a
        # "filing exists" anchor.
        _mark_enriched(f["id"], len(rows))
        time.sleep(pause_between)

    return {
        "ok": True,
        "filings_seen": len(filings),
        "parsed": parsed_count,
        "txs_written": txs_written,
        "parse_failures": parse_failures,
        "fetch_failures": fetch_failures,
    }


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run_ingest(only_since_filed_days=30), indent=2, default=str))
    print("--- enrichment pass ---")
    print(json.dumps(enrich_house_filings(max_filings=5), indent=2))
