#!/usr/bin/env python3
"""
SEC EDGAR Form 4 ingester — corporate insider trades.

Pulls the "current Form 4 filings" atom feed, fetches each filing's primary
ownership XML, parses the non-derivative + derivative transaction tables,
and lands them into `insider_events` with venue='sec_form4'.

Latency: filings appear in EDGAR T+0 to T+2 from the actual transaction.
Scope:   officers, directors, 10% owners of US-listed companies.

SEC compliance (https://www.sec.gov/os/accessing-edgar-data):
  - User-Agent MUST identify the requester (name + email). The server
    returns 403 to generic UAs. We require SEC_USER_AGENT env var and
    no-op gracefully if it's missing.
  - Stay under 10 req/sec. We pace to ~5 req/s with RATE_PAUSE.

Falls back gracefully when SEC_USER_AGENT isn't set: `is_available()`
returns False and `run_ingest()` returns a no-op result, mirroring the
pattern used in wallet_metadata.py for POLYGONSCAN_API_KEY.
"""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

import insider_events

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────
ATOM_FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&output=atom&start=0&count=100"
)
EDGAR_BASE = "https://www.sec.gov"
HTTP_TIMEOUT = 15.0
RATE_PAUSE = 0.21          # ~5 req/s — well under the 10 req/s limit
MAX_FILINGS_PER_RUN = 100  # cap each poll to avoid stampedes after downtime

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
# Form 4 XML uses no namespace for the root in most filings, but defensively
# we look up the leaf tag name regardless of any namespace prefix.


def _user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", "").strip()


def is_available() -> bool:
    return bool(_user_agent())


def _client() -> httpx.Client:
    """SEC requires UA + Host. Accept-Encoding is recommended."""
    return httpx.Client(
        timeout=HTTP_TIMEOUT,
        headers={
            "User-Agent": _user_agent(),
            "Accept-Encoding": "gzip, deflate",
            "Host": "www.sec.gov",
        },
        follow_redirects=True,
    )


# ─── Atom feed ────────────────────────────────────────────────────────

_ACCESSION_RE = re.compile(r"accession-number=([\d\-]+)", re.IGNORECASE)
_CIK_FROM_HREF = re.compile(r"/Archives/edgar/data/(\d+)/", re.IGNORECASE)


def _fetch_atom(client: httpx.Client) -> list[dict]:
    """Return entries from the current-Form-4 atom feed."""
    r = client.get(ATOM_FEED)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out: list[dict] = []
    for entry in root.findall("a:entry", ATOM_NS):
        eid = (entry.findtext("a:id", default="", namespaces=ATOM_NS) or "").strip()
        title = (entry.findtext("a:title", default="", namespaces=ATOM_NS) or "").strip()
        link_el = entry.find("a:link", ATOM_NS)
        href = link_el.get("href") if link_el is not None else ""
        updated = (entry.findtext("a:updated", default="", namespaces=ATOM_NS) or "").strip()

        m = _ACCESSION_RE.search(eid) or _ACCESSION_RE.search(href or "")
        if not m:
            continue
        accession = m.group(1)
        cik_match = _CIK_FROM_HREF.search(href or "")
        cik = cik_match.group(1) if cik_match else None
        out.append({
            "accession": accession,
            "cik": cik,
            "title": title,
            "index_href": href,
            "updated": updated,
        })
    return out


def _parse_iso8601(s: str) -> int | None:
    """Parse atom <updated> or YYYY-MM-DD into a unix timestamp."""
    if not s:
        return None
    try:
        # Try full ISO-8601 first (atom feed format)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        pass
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


# ─── Filing fetch + parse ────────────────────────────────────────────

def _filing_index_json_url(cik: str, accession: str) -> str:
    """
    The atom feed gives us an HTML index URL; the JSON sibling is more
    structured. Path: /Archives/edgar/data/{cik}/{accession-no-dashes}/index.json
    """
    accession_clean = accession.replace("-", "")
    return f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{accession_clean}/index.json"


def _find_primary_xml(client: httpx.Client, cik: str, accession: str) -> str | None:
    """
    Locate the ownership-document XML inside a Form 4 filing.

    EDGAR puts several files in each filing; we want the human-authored
    ownership XML, not the auto-generated R*.xml render files. The primary
    doc is usually named primary_doc.xml or wf-form4_*.xml.
    """
    try:
        r = client.get(_filing_index_json_url(cik, accession))
        r.raise_for_status()
        items = r.json().get("directory", {}).get("item", []) or []
    except Exception as e:
        logger.warning("EDGAR index fetch failed for %s: %s", accession, e)
        return None

    candidates = [
        it.get("name", "") for it in items
        if it.get("name", "").lower().endswith(".xml")
        and not it.get("name", "").startswith(("R", "Financial_Report"))
    ]
    if not candidates:
        return None
    # Prefer canonical names
    for pref in ("primary_doc.xml",):
        if pref in candidates:
            chosen = pref
            break
    else:
        # Pick the shortest name that contains form4 or fallback to first
        form4 = [c for c in candidates if "form4" in c.lower()]
        chosen = (form4 or candidates)[0]

    accession_clean = accession.replace("-", "")
    return f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{accession_clean}/{chosen}"


def _local(tag: str) -> str:
    """Strip XML namespace prefix from a tag name."""
    return tag.rsplit("}", 1)[-1]


def _findtext_local(elem: ET.Element, *path: str) -> str | None:
    """Walk a path of namespace-agnostic tag names, return text or None."""
    cur: ET.Element | None = elem
    for name in path:
        if cur is None:
            return None
        nxt = None
        for child in cur:
            if _local(child.tag) == name:
                nxt = child
                break
        cur = nxt
    if cur is None or cur.text is None:
        return None
    return cur.text.strip() or None


def _safe_float(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _parse_form4_xml(xml_bytes: bytes) -> dict[str, Any] | None:
    """
    Parse a Form 4 ownership document. Returns:
      {
        "issuer": {"cik", "name", "ticker"},
        "owners": [{"cik", "name", "is_director", "is_officer",
                    "is_ten_percent", "officer_title"}, ...],
        "transactions": [
          {"kind": "non_derivative" | "derivative",
           "security_title", "transaction_date", "shares",
           "price_per_share", "acquired_disposed", "transaction_code"}, ...
        ]
      }
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("Form 4 XML parse error: %s", e)
        return None

    # Issuer
    issuer_cik = _findtext_local(root, "issuer", "issuerCik")
    issuer_name = _findtext_local(root, "issuer", "issuerName")
    issuer_ticker = _findtext_local(root, "issuer", "issuerTradingSymbol")

    # Reporting owners (can be multiple)
    owners: list[dict] = []
    for child in root:
        if _local(child.tag) != "reportingOwner":
            continue
        owner_cik = _findtext_local(child, "reportingOwnerId", "rptOwnerCik")
        owner_name = _findtext_local(child, "reportingOwnerId", "rptOwnerName")
        is_director = (_findtext_local(child, "reportingOwnerRelationship", "isDirector") or "").strip()
        is_officer = (_findtext_local(child, "reportingOwnerRelationship", "isOfficer") or "").strip()
        is_ten = (_findtext_local(child, "reportingOwnerRelationship", "isTenPercentOwner") or "").strip()
        officer_title = _findtext_local(child, "reportingOwnerRelationship", "officerTitle")
        owners.append({
            "cik": owner_cik,
            "name": owner_name,
            "is_director": is_director in ("1", "true", "True"),
            "is_officer": is_officer in ("1", "true", "True"),
            "is_ten_percent": is_ten in ("1", "true", "True"),
            "officer_title": officer_title,
        })

    # Transactions — both non-derivative (stock) and derivative (options)
    txs: list[dict] = []

    def _parse_tx_row(tx_elem: ET.Element, kind: str) -> dict | None:
        sec_title = _findtext_local(tx_elem, "securityTitle", "value")
        tx_date = _findtext_local(tx_elem, "transactionDate", "value")
        shares = _safe_float(_findtext_local(tx_elem, "transactionAmounts", "transactionShares", "value"))
        price = _safe_float(_findtext_local(tx_elem, "transactionAmounts", "transactionPricePerShare", "value"))
        acq_disp = _findtext_local(tx_elem, "transactionAmounts", "transactionAcquiredDisposedCode", "value")
        tx_code = _findtext_local(tx_elem, "transactionCoding", "transactionCode")
        if shares is None and tx_date is None:
            return None
        return {
            "kind": kind,
            "security_title": sec_title,
            "transaction_date": tx_date,
            "shares": shares,
            "price_per_share": price,
            "acquired_disposed": acq_disp,
            "transaction_code": tx_code,
        }

    def _walk_table(table_name: str, tx_name: str, kind: str) -> None:
        for table in root:
            if _local(table.tag) != table_name:
                continue
            for tx in table:
                if _local(tx.tag) != tx_name:
                    continue
                row = _parse_tx_row(tx, kind)
                if row:
                    txs.append(row)

    _walk_table("nonDerivativeTable", "nonDerivativeTransaction", "non_derivative")
    _walk_table("derivativeTable", "derivativeTransaction", "derivative")

    return {
        "issuer": {"cik": issuer_cik, "name": issuer_name, "ticker": issuer_ticker},
        "owners": owners,
        "transactions": txs,
    }


# ─── Mapping → insider_events rows ───────────────────────────────────

def _classify_side(kind: str, acq_disp: str | None, tx_code: str | None) -> str:
    """
    Map (kind, A/D, transaction code) → side.

    Form 4 transactionCode values:
      P = Purchase (open market)         → buy
      S = Sale (open market)             → sell
      A = Grant/award                    → exchange (compensation, not a market signal)
      M = Exercise of derivative         → exchange
      F = Tax withholding                → exchange
      G = Gift                           → gift
      D = Disposition (other)            → sell
      X = Exercise of in-the-money       → option_buy / exchange
    """
    # Filter non-market-signal codes first — these happen on both
    # derivative and non-derivative rows and are pure compensation/admin.
    if tx_code == "G":
        return "gift"
    if tx_code in ("A", "M", "F", "X"):
        return "exchange"
    if kind == "derivative":
        if acq_disp == "A":
            return "option_buy"
        if acq_disp == "D":
            return "option_sell"
    if acq_disp == "A":
        return "buy"
    if acq_disp == "D":
        return "sell"
    return "other"


def _primary_owner_role(owner: dict) -> str:
    bits: list[str] = []
    if owner.get("officer_title"):
        bits.append(owner["officer_title"])
    elif owner.get("is_officer"):
        bits.append("Officer")
    if owner.get("is_director"):
        bits.append("Director")
    if owner.get("is_ten_percent"):
        bits.append("10% Owner")
    return ", ".join(bits) if bits else "Insider"


def _build_event_rows(
    parsed: dict, accession: str, ts_filed: int | None, raw_url: str,
) -> list[dict]:
    """One Form 4 → potentially many insider_events rows (per tx × per owner)."""
    issuer = parsed.get("issuer") or {}
    owners = parsed.get("owners") or []
    txs = parsed.get("transactions") or []
    if not owners or not txs:
        return []

    ticker = (issuer.get("ticker") or "").strip().upper() or None
    issuer_name = issuer.get("name")

    rows: list[dict] = []
    for owner in owners:
        owner_id = owner.get("cik") or owner.get("name") or "unknown"
        owner_label = owner.get("name") or owner_id
        role = _primary_owner_role(owner)

        for i, tx in enumerate(txs):
            ts_executed = _parse_iso8601(tx.get("transaction_date") or "")
            shares = tx.get("shares")
            price = tx.get("price_per_share")
            usd = (shares * price) if (shares and price) else None
            side = _classify_side(tx["kind"], tx.get("acquired_disposed"), tx.get("transaction_code"))

            # Composite source_id so multiple txs / owners in the same filing
            # don't collide on the (venue, source_id) UNIQUE constraint.
            sid = f"{accession}:{owner_id}:{i}"

            rows.append({
                "venue": "sec_form4",
                "source_id": sid,
                "ts_filed": ts_filed,
                "ts_executed": ts_executed,
                "actor_id": f"CIK{owner_id}",
                "actor_label": owner_label,
                "actor_role": role,
                "symbol": ticker,
                "symbol_name": issuer_name,
                "side": side,
                "shares": shares,
                "price": price,
                "size_usd_low": usd,
                "size_usd_high": usd,
                "raw_url": raw_url,
                "extra": {
                    "kind": tx["kind"],
                    "security_title": tx.get("security_title"),
                    "transaction_code": tx.get("transaction_code"),
                    "acquired_disposed": tx.get("acquired_disposed"),
                    "issuer_cik": issuer.get("cik"),
                    "accession": accession,
                },
            })
    return rows


# ─── Top-level run ───────────────────────────────────────────────────

def run_ingest(max_filings: int = MAX_FILINGS_PER_RUN) -> dict:
    """
    Poll the atom feed, parse new Form 4 filings, land rows in insider_events.

    Returns a summary dict:
      {available, filings_seen, filings_parsed, inserted, skipped, errors}
    """
    if not is_available():
        return {
            "available": False,
            "reason": "SEC_USER_AGENT not set",
            "filings_seen": 0, "filings_parsed": 0,
            "inserted": 0, "skipped": 0, "errors": 0,
        }

    insider_events.init_db()
    filings_parsed = 0
    inserted = skipped = errors = 0

    with _client() as client:
        try:
            feed = _fetch_atom(client)
        except Exception as e:
            logger.warning("EDGAR atom feed fetch failed: %s", e)
            return {
                "available": True, "reason": f"feed: {e}",
                "filings_seen": 0, "filings_parsed": 0,
                "inserted": 0, "skipped": 0, "errors": 1,
            }
        filings_seen = len(feed)
        time.sleep(RATE_PAUSE)

        for entry in feed[:max_filings]:
            cik = entry.get("cik")
            accession = entry.get("accession")
            if not cik or not accession:
                continue

            xml_url = _find_primary_xml(client, cik, accession)
            time.sleep(RATE_PAUSE)
            if not xml_url:
                continue

            try:
                rx = client.get(xml_url)
                rx.raise_for_status()
                parsed = _parse_form4_xml(rx.content)
            except Exception as e:
                logger.warning("Form 4 fetch/parse failed for %s: %s", accession, e)
                errors += 1
                time.sleep(RATE_PAUSE)
                continue
            time.sleep(RATE_PAUSE)
            if not parsed:
                continue

            ts_filed = _parse_iso8601(entry.get("updated") or "")
            rows = _build_event_rows(parsed, accession, ts_filed, xml_url)
            if not rows:
                continue
            res = insider_events.upsert_many(rows)
            inserted += res["inserted"]
            skipped += res["skipped"]
            errors += res["errors"]
            filings_parsed += 1

    return {
        "available": True,
        "filings_seen": filings_seen,
        "filings_parsed": filings_parsed,
        "inserted": inserted,
        "skipped": skipped,
        "errors": errors,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("SEC_USER_AGENT set:", is_available())
    if is_available():
        import json
        print(json.dumps(run_ingest(max_filings=5), indent=2))
