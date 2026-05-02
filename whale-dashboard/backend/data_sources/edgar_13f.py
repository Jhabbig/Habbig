from __future__ import annotations
"""EDGAR 13F-HR ingester.

13F-HR ("Holdings Report") is filed quarterly by institutional investment
managers with >$100M AUM. It lists US-equity long positions and certain
options, reported as of quarter-end with a 45-day filing deadline.

Pipeline per CIK:
    1. GET data.sec.gov/submissions/CIK{cik:010d}.json
       -> list of all filings; filter formType in {13F-HR, 13F-HR/A}
    2. For each new accession, fetch the filing index:
       https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/
       -> find the InfoTable XML (the actual positions)
    3. Parse the InfoTable XML (post-2013 it's structured XML; pre-2013 was
       free-form HTML and is out of scope for this ingester).
    4. Insert the filing row + per-position rows.
    5. After ingest, run diff_engine to compute Q-over-Q deltas.

SEC rules require a descriptive User-Agent with contact info, and impose a
soft rate limit of ~10 req/sec. We sleep ~120ms between requests to stay
well under the limit.

References:
    https://www.sec.gov/os/accessing-edgar-data
    https://www.sec.gov/divisions/investment/13ffaq
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

import aiohttp
from lxml import etree

from database import get_conn

logger = logging.getLogger(__name__)

SEC_BASE = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
RATE_DELAY_S = 0.13  # ~7.5 req/sec, well under the 10/s SEC ceiling

# Accept any of the namespaces SEC has used over time for the InfoTable.
# Older filings used eis_Common; current uses informationtable.
_NS = {
    "ns1": "http://www.sec.gov/edgar/document/thirteenf/informationtable",
    "ns2": "http://www.sec.gov/edgar/thirteenffiler",
}


def _user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        # SEC will rate-limit aggressively without a real UA. Fail loudly.
        raise RuntimeError(
            "SEC_USER_AGENT env var is required (e.g. 'WhaleDashboard contact@example.com')"
        )
    return ua


@dataclass
class FilingRef:
    cik: int
    accession: str        # with dashes, e.g. "0001104659-25-001234"
    form_type: str        # "13F-HR" or "13F-HR/A"
    filed_date: str       # ISO date
    quarter_end: str      # ISO date (period of report)


@dataclass
class Position:
    cusip: str
    issuer_name: str
    title_of_class: Optional[str]
    shares: int
    value_usd: float
    put_call: Optional[str]
    investment_disc: Optional[str]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

async def _get(session: aiohttp.ClientSession, url: str) -> str:
    """GET as text with SEC-compliant headers and gentle pacing."""
    await asyncio.sleep(RATE_DELAY_S)
    async with session.get(url, headers={"User-Agent": _user_agent(),
                                         "Accept-Encoding": "gzip, deflate"}) as r:
        r.raise_for_status()
        return await r.text()


async def _get_bytes(session: aiohttp.ClientSession, url: str) -> bytes:
    await asyncio.sleep(RATE_DELAY_S)
    async with session.get(url, headers={"User-Agent": _user_agent(),
                                         "Accept-Encoding": "gzip, deflate"}) as r:
        r.raise_for_status()
        return await r.read()


# ---------------------------------------------------------------------------
# Filing discovery
# ---------------------------------------------------------------------------

async def list_13f_filings(session: aiohttp.ClientSession,
                           cik: int) -> List[FilingRef]:
    """Return all 13F-HR/HR/A filings for a CIK from the submissions JSON."""
    import json as _json
    url = f"{SEC_DATA}/submissions/CIK{cik:010d}.json"
    text = await _get(session, url)
    data = _json.loads(text)
    recent = data.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filed_dates = recent.get("filingDate", [])
    period_dates = recent.get("reportDate", [])

    out: List[FilingRef] = []
    for form, acc, filed, period in zip(forms, accessions, filed_dates, period_dates):
        if form not in ("13F-HR", "13F-HR/A"):
            continue
        if not period:
            continue
        out.append(FilingRef(
            cik=cik, accession=acc, form_type=form,
            filed_date=filed, quarter_end=period,
        ))
    return out


async def find_information_table_url(session: aiohttp.ClientSession,
                                     cik: int, accession: str) -> Optional[str]:
    """Locate the InfoTable XML inside a filing's archive directory."""
    nodash = accession.replace("-", "")
    index_url = f"{SEC_BASE}/Archives/edgar/data/{cik}/{nodash}/"
    html = await _get(session, index_url)
    # The index page lists files as anchors; look for any *.xml that is NOT
    # the primary_doc.xml (which is the cover page).
    candidates = re.findall(r'href="([^"]+\.xml)"', html, flags=re.IGNORECASE)
    info_xml: Optional[str] = None
    for c in candidates:
        name = c.rsplit("/", 1)[-1].lower()
        if name in ("primary_doc.xml",):
            continue
        # Heuristic: most filers name it informationtable.xml or similar
        if "info" in name or "table" in name or "form13f" in name:
            info_xml = c
            break
    if not info_xml and candidates:
        # Fall back to first non-primary XML.
        for c in candidates:
            if "primary_doc" not in c.lower():
                info_xml = c
                break
    if not info_xml:
        return None
    if info_xml.startswith("/"):
        return f"{SEC_BASE}{info_xml}"
    return f"{index_url}{info_xml}"


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def parse_information_table(xml_bytes: bytes) -> List[Position]:
    """Parse a 13F-HR InformationTable XML into Position rows.

    The schema is namespaced but filers vary, so we strip namespaces and walk
    by local-name. Each `infoTable` element has nameOfIssuer, titleOfClass,
    cusip, value, shrsOrPrnAmt/sshPrnamt, putCall (optional), investmentDiscretion.

    Note: `value` was reported in thousands of USD prior to Q4 2022 amendment;
    SEC now requires raw USD. We treat the reported value as raw — most modern
    filings comply, and underreporting is preferable to overcounting if a filer
    is non-compliant.
    """
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(xml_bytes, parser=parser)
    positions: List[Position] = []

    # Strip namespaces for sanity.
    for elem in root.iter():
        if isinstance(elem.tag, str) and "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    for it in root.iter("infoTable"):
        def _txt(name: str) -> Optional[str]:
            el = it.find(name)
            return el.text.strip() if el is not None and el.text else None

        cusip = _txt("cusip")
        issuer = _txt("nameOfIssuer")
        if not cusip or not issuer:
            continue
        title_class = _txt("titleOfClass")

        # Shares: shrsOrPrnAmt > sshPrnamt
        shares = 0
        sopa = it.find("shrsOrPrnAmt")
        if sopa is not None:
            ssh = sopa.find("sshPrnamt")
            if ssh is not None and ssh.text:
                try:
                    shares = int(ssh.text.replace(",", "").strip())
                except ValueError:
                    shares = 0

        value_raw = _txt("value")
        try:
            value_usd = float(value_raw.replace(",", "")) if value_raw else 0.0
        except ValueError:
            value_usd = 0.0

        put_call = _txt("putCall")
        invest_disc = _txt("investmentDiscretion")

        positions.append(Position(
            cusip=cusip.upper(),
            issuer_name=issuer,
            title_of_class=title_class,
            shares=shares,
            value_usd=value_usd,
            put_call=put_call,
            investment_disc=invest_disc,
        ))

    return positions


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _filing_already_ingested(accession: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM filings_13f WHERE accession=?", (accession,)
        ).fetchone()
        return row is not None


def _insert_filing(ref: FilingRef, positions: List[Position]) -> int:
    """Insert filing + holdings in a single transaction. Returns filing_id."""
    total_value = sum(p.value_usd for p in positions)
    n = len(positions)
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO filings_13f
                 (cik, accession, form_type, quarter_end, filed_date,
                  total_value_usd, n_positions, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref.cik, ref.accession, ref.form_type, ref.quarter_end,
             ref.filed_date, total_value, n, now),
        )
        filing_id = cur.lastrowid

        # Resolve ticker via cusip_ticker (8-char prefix) where available.
        cusip8s = list({p.cusip[:8] for p in positions})
        ticker_map: dict[str, str] = {}
        if cusip8s:
            qmarks = ",".join("?" * len(cusip8s))
            for r in conn.execute(
                f"SELECT cusip8, ticker FROM cusip_ticker WHERE cusip8 IN ({qmarks})",
                cusip8s,
            ):
                ticker_map[r["cusip8"]] = r["ticker"]

        rows = [
            (filing_id, p.cusip, ticker_map.get(p.cusip[:8]), p.issuer_name,
             p.title_of_class, p.shares, p.value_usd, p.put_call, p.investment_disc)
            for p in positions
        ]
        conn.executemany(
            """INSERT OR IGNORE INTO holdings
                 (filing_id, cusip, ticker, issuer_name, title_of_class,
                  shares, value_usd, put_call, investment_disc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        return int(filing_id)


# ---------------------------------------------------------------------------
# Top-level ingest
# ---------------------------------------------------------------------------

async def ingest_cik(session: aiohttp.ClientSession, cik: int) -> Tuple[int, int]:
    """Ingest all new 13F filings for a CIK. Returns (n_new, n_skipped)."""
    refs = await list_13f_filings(session, cik)
    n_new = 0
    n_skipped = 0
    for ref in refs:
        if _filing_already_ingested(ref.accession):
            n_skipped += 1
            continue
        try:
            xml_url = await find_information_table_url(session, cik, ref.accession)
            if not xml_url:
                logger.warning("13f: no InfoTable for cik=%d accession=%s",
                               cik, ref.accession)
                continue
            xml_bytes = await _get_bytes(session, xml_url)
            positions = parse_information_table(xml_bytes)
            if not positions:
                logger.warning("13f: empty positions for cik=%d accession=%s",
                               cik, ref.accession)
                continue
            _insert_filing(ref, positions)
            n_new += 1
            logger.info("13f: ingested cik=%d quarter=%s n_positions=%d",
                        cik, ref.quarter_end, len(positions))
        except Exception:
            logger.exception("13f: failed cik=%d accession=%s", cik, ref.accession)
    return n_new, n_skipped


def _seeded_ciks() -> Iterable[int]:
    with get_conn() as conn:
        return [int(r["cik"]) for r in conn.execute(
            "SELECT cik FROM cik_map WHERE filing_authority IN ('13F','all')"
        ).fetchall()]


async def ingest_seeded_entities() -> dict:
    """Ingest 13F filings for every CIK currently in cik_map.

    Records a row in ingest_runs so admins can see what happened.
    """
    started = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_runs (source, started_at, status) VALUES (?, ?, 'running')",
            ("edgar_13f", started),
        )
        run_id = cur.lastrowid

    total_new = 0
    total_skipped = 0
    error: Optional[str] = None
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for cik in _seeded_ciks():
                try:
                    new, skipped = await ingest_cik(session, cik)
                    total_new += new
                    total_skipped += skipped
                except Exception as e:
                    logger.exception("13f: cik=%d failed", cik)
                    error = f"{type(e).__name__}: {e}"
    finally:
        finished = datetime.now(timezone.utc).isoformat()
        status = "error" if error else "ok"
        with get_conn() as conn:
            conn.execute(
                """UPDATE ingest_runs
                      SET finished_at=?, status=?, n_new=?, error=?
                    WHERE id=?""",
                (finished, status, total_new, error, run_id),
            )

    # Fire-and-forget Q-over-Q diff computation.
    try:
        from analysis.diff_engine import recompute_all_deltas
        recompute_all_deltas()
    except Exception:
        logger.exception("13f: diff engine post-step failed")

    return {"new": total_new, "skipped": total_skipped, "error": error}
