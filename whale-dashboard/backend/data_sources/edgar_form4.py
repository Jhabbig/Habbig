from __future__ import annotations
"""Form 4 ingester (insider transactions).

Form 4 is filed BY the insider (officer / director / 10%+ owner) and lists
transactions in the issuer's stock. EDGAR cross-indexes by issuer, so we can
poll per-issuer via the atom feed:

    https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={issuer}
        &type=4&dateb=&owner=include&count=40&output=atom

For each Form 4 in the feed we fetch the ownership XML and pull out:
    - reporting owner (CIK, name, role)
    - non-derivative transactions (txn_date, txn_code, shares, price)
    - derivative transactions are skipped in the headline ingest — they're
      important for some signals but noisy and require option-aware logic
      to interpret. Stored only via accession + insider_name in v1.

txn_code legend (most common):
    P  open-market purchase     — strong buy signal
    S  open-market sale
    A  grant / award
    M  exercise of derivative
    F  payment of tax via withholding shares
    G  gift
    D  return to issuer
We surface P and S in the UI; the rest are noise for "movement" framing.

This module relies on issuer_watchlist being populated. Call
cusip_seeder.refresh_issuer_watchlist() before the first Form 4 poll.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import aiohttp
from lxml import etree

from database import get_conn

logger = logging.getLogger(__name__)

SEC_BASE = "https://www.sec.gov"
RATE_DELAY_S = 0.13


def _user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError("SEC_USER_AGENT env var is required")
    return ua


async def _get_text(session: aiohttp.ClientSession, url: str) -> str:
    await asyncio.sleep(RATE_DELAY_S)
    async with session.get(url, headers={"User-Agent": _user_agent()}) as r:
        r.raise_for_status()
        return await r.text()


# ---------------------------------------------------------------------------
# Atom feed -> list of (accession, filing index URL)
# ---------------------------------------------------------------------------

_ACCESSION_RE = re.compile(r"accession_number=(\d{10}-\d{2}-\d{6})", re.IGNORECASE)
_HREF_RE = re.compile(r"<link[^>]+href=\"([^\"]+)\"")


async def list_form4_for_issuer(session: aiohttp.ClientSession,
                                issuer_cik: int,
                                count: int = 40) -> List[Tuple[str, str]]:
    """Return (accession, index_url) tuples for recent Form 4s of an issuer."""
    url = (f"{SEC_BASE}/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={issuer_cik:010d}&type=4&dateb=&owner=include"
           f"&count={count}&output=atom")
    try:
        atom = await _get_text(session, url)
    except aiohttp.ClientResponseError as e:
        if e.status == 404:
            return []
        raise

    out: List[Tuple[str, str]] = []
    seen: set[str] = set()
    # Iterate hrefs in document order — atom <entry> contains a <link href="...">
    # to the filing index page. We extract the accession from that URL.
    for href_match in _HREF_RE.finditer(atom):
        href = href_match.group(1)
        m = _ACCESSION_RE.search(href)
        if not m:
            continue
        acc = m.group(1)
        if acc in seen:
            continue
        seen.add(acc)
        out.append((acc, href))
    return out


# ---------------------------------------------------------------------------
# Form 4 XML parsing
# ---------------------------------------------------------------------------

async def find_form4_xml_url(session: aiohttp.ClientSession,
                             cik: int, accession: str) -> Optional[str]:
    """Locate the ownershipDocument XML inside a Form 4 filing."""
    nodash = accession.replace("-", "")
    index_url = f"{SEC_BASE}/Archives/edgar/data/{cik}/{nodash}/"
    html = await _get_text(session, index_url)
    candidates = re.findall(r'href="([^"]+\.xml)"', html, flags=re.IGNORECASE)
    # Form 4 ownershipDocument is usually wf-form4_*.xml or similar.
    for c in candidates:
        name = c.rsplit("/", 1)[-1].lower()
        if name == "primary_doc.xml":
            continue
        if "form4" in name or "ownership" in name or name.startswith("wf-"):
            return f"{SEC_BASE}{c}" if c.startswith("/") else f"{index_url}{c}"
    # Fallback: any non-primary_doc xml
    for c in candidates:
        if "primary_doc" not in c.lower():
            return f"{SEC_BASE}{c}" if c.startswith("/") else f"{index_url}{c}"
    return None


def parse_form4_xml(xml_text: str) -> dict:
    """Extract issuer + reporting owner + non-derivative transactions."""
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(xml_text.encode("utf-8"), parser=parser)
    # Strip namespaces.
    for elem in root.iter():
        if isinstance(elem.tag, str) and "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    def _txt(parent, path: str) -> Optional[str]:
        if parent is None:
            return None
        el = parent.find(path)
        if el is None:
            return None
        # Many fields wrap the value in <value>foo</value>
        v = el.find("value")
        if v is not None and v.text:
            return v.text.strip()
        return el.text.strip() if el.text else None

    issuer_el = root.find("issuer")
    issuer_cik = _txt(issuer_el, "issuerCik")
    issuer_name = _txt(issuer_el, "issuerName")
    issuer_ticker = _txt(issuer_el, "issuerTradingSymbol")

    owner_el = root.find("reportingOwner")
    insider_cik = _txt(owner_el, "reportingOwnerId/rptOwnerCik") if owner_el is not None else None
    insider_name = _txt(owner_el, "reportingOwnerId/rptOwnerName") if owner_el is not None else None

    relationship_el = owner_el.find("reportingOwnerRelationship") if owner_el is not None else None
    roles = []
    if relationship_el is not None:
        for r_field, r_label in [
            ("isDirector", "Director"),
            ("isOfficer", "Officer"),
            ("isTenPercentOwner", "10%+ Owner"),
            ("isOther", "Other"),
        ]:
            v = relationship_el.find(r_field)
            if v is not None and (v.text or "").strip() in ("1", "true", "True"):
                roles.append(r_label)
        officer_title = _txt(relationship_el, "officerTitle")
        if officer_title and "Officer" in roles:
            roles[roles.index("Officer")] = f"Officer ({officer_title})"

    transactions = []
    nd_table = root.find("nonDerivativeTable")
    if nd_table is not None:
        for txn in nd_table.findall("nonDerivativeTransaction"):
            txn_date = _txt(txn, "transactionDate")
            coding = txn.find("transactionCoding")
            txn_code = _txt(coding, "transactionCode")
            amounts = txn.find("transactionAmounts")
            shares_raw = _txt(amounts, "transactionShares")
            price_raw = _txt(amounts, "transactionPricePerShare")
            post = txn.find("postTransactionAmounts")
            post_shares_raw = _txt(post, "sharesOwnedFollowingTransaction")
            try:
                shares = float((shares_raw or "0").replace(",", ""))
            except ValueError:
                shares = 0.0
            try:
                price = float((price_raw or "0").replace(",", "")) if price_raw else None
            except ValueError:
                price = None
            try:
                post_shares = float((post_shares_raw or "0").replace(",", "")) if post_shares_raw else None
            except ValueError:
                post_shares = None
            transactions.append({
                "txn_date": txn_date,
                "txn_code": txn_code,
                "shares": shares,
                "price": price,
                "post_holdings": post_shares,
            })

    return {
        "issuer_cik": int(issuer_cik) if issuer_cik else None,
        "issuer_name": issuer_name,
        "issuer_ticker": (issuer_ticker or "").upper() or None,
        "insider_cik": int(insider_cik) if insider_cik else None,
        "insider_name": insider_name,
        "insider_role": ", ".join(roles) if roles else None,
        "transactions": transactions,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist(accession: str, parsed: dict) -> int:
    """Insert all non-derivative transactions from a parsed Form 4. Returns
    the number of rows actually inserted (excludes duplicates)."""
    if not parsed.get("issuer_cik") or not parsed.get("insider_name"):
        return 0
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (accession, parsed["issuer_cik"], parsed.get("issuer_ticker"),
         parsed.get("issuer_name"), parsed.get("insider_cik"),
         parsed["insider_name"], parsed.get("insider_role"),
         t["txn_date"], t["txn_code"], t["shares"], t["price"],
         (t["shares"] * t["price"]) if (t["shares"] and t["price"]) else None,
         t["post_holdings"], now)
        for t in parsed["transactions"] if t.get("txn_date")
    ]
    if not rows:
        return 0
    with get_conn() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO insider_txns
                 (accession, issuer_cik, issuer_ticker, issuer_name,
                  insider_cik, insider_name, insider_role,
                  txn_date, txn_code, shares, price, value_usd,
                  post_holdings, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Top-level ingest
# ---------------------------------------------------------------------------

async def ingest_issuer_form4(session: aiohttp.ClientSession,
                              issuer_cik: int) -> int:
    """Ingest recent Form 4s for one issuer. Returns rows inserted."""
    refs = await list_form4_for_issuer(session, issuer_cik)
    inserted = 0
    for accession, _href in refs:
        # Skip if we already have any rows for this accession.
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM insider_txns WHERE accession=? LIMIT 1", (accession,)
            ).fetchone()
        if existing:
            continue
        try:
            xml_url = await find_form4_xml_url(session, issuer_cik, accession)
            if not xml_url:
                continue
            xml_text = await _get_text(session, xml_url)
            parsed = parse_form4_xml(xml_text)
            inserted += _persist(accession, parsed)
        except Exception:
            logger.exception("form4: failed accession=%s issuer=%d", accession, issuer_cik)

    with get_conn() as conn:
        conn.execute(
            "UPDATE issuer_watchlist SET last_form4_check=? WHERE cik=?",
            (datetime.now(timezone.utc).isoformat(), issuer_cik),
        )
    return inserted


async def ingest_watchlist(limit: Optional[int] = None) -> dict:
    """Iterate the issuer watchlist and ingest Form 4s for each. We rotate
    by `last_form4_check` so the same issuers don't always come first."""
    started = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_runs (source, started_at, status) VALUES (?, ?, 'running')",
            ("edgar_form4", started),
        )
        run_id = cur.lastrowid

        rows = conn.execute(
            """SELECT cik FROM issuer_watchlist
                ORDER BY COALESCE(last_form4_check, '1970-01-01') ASC
                LIMIT ?""",
            (limit if limit else 99999,),
        ).fetchall()

    total = 0
    error: Optional[str] = None
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for r in rows:
                try:
                    total += await ingest_issuer_form4(session, int(r["cik"]))
                except Exception as e:
                    logger.exception("form4: cik=%s failed", r["cik"])
                    error = f"{type(e).__name__}: {e}"
    finally:
        finished = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute(
                """UPDATE ingest_runs SET finished_at=?, status=?, n_new=?, error=?
                    WHERE id=?""",
                (finished, "error" if error else "ok", total, error, run_id),
            )
    return {"new_txns": total, "issuers_checked": len(rows), "error": error}
