from __future__ import annotations
"""Schedule 13D / 13G ingester.

13D = filed when a person/entity acquires beneficial ownership of more than
5% of a public company's voting equity AND has activist intent.
13G = same threshold but passive intent (institutions, e.g. Vanguard hold
many 13G positions for index funds).

Like Form 4, 13D/G is filed by the holder but cross-indexed against the
issuer. We poll per-issuer with the same atom-feed pattern used by
edgar_form4.py.

Unlike Form 4, the 13D body is unstructured prose (Items 1-7). We extract
what we can from the cover page (filer CIK, target, ownership %, event date)
and store the raw text for Item 4 (Purpose of Transaction). Doing real NLP
on Item 4 to score "activist intent" is a v2 problem; the v1 dashboard
shows the filing + raw Item 4 text and lets the user judge.

Filer entity attribution: we resolve the filer CIK against entities/cik_map.
If the filer isn't in our seed list (e.g., a new activist), entity_resolver
auto-creates a low-confidence entity which we surface for manual review.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import aiohttp

from analysis.entity_resolver import resolve as resolve_entity
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


_ACCESSION_RE = re.compile(r"accession_number=(\d{10}-\d{2}-\d{6})", re.IGNORECASE)
_HREF_RE = re.compile(r"<link[^>]+href=\"([^\"]+)\"")
_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
_CATEGORY_RE = re.compile(r'<category[^>]*term="([^"]+)"')


async def list_13d_for_issuer(session: aiohttp.ClientSession,
                              issuer_cik: int,
                              count: int = 40) -> List[Tuple[str, str, str]]:
    """Return (accession, schedule, index_url) for recent 13D/G filings.

    `schedule` is one of "13D", "13G", "13D/A", "13G/A".
    """
    out: List[Tuple[str, str, str]] = []
    seen: set[str] = set()
    # The atom feed accepts type=SC%2013D or type=SC%2013G. We query both.
    for type_query, schedule_label in [("SC%2013D", "13D"), ("SC%2013G", "13G")]:
        url = (f"{SEC_BASE}/cgi-bin/browse-edgar?action=getcompany"
               f"&CIK={issuer_cik:010d}&type={type_query}&dateb=&owner=include"
               f"&count={count}&output=atom")
        try:
            atom = await _get_text(session, url)
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                continue
            raise

        for entry in _ENTRY_RE.findall(atom):
            href_m = _HREF_RE.search(entry)
            cat_m = _CATEGORY_RE.search(entry)
            if not href_m:
                continue
            href = href_m.group(1)
            acc_m = _ACCESSION_RE.search(href)
            if not acc_m:
                continue
            acc = acc_m.group(1)
            if acc in seen:
                continue
            seen.add(acc)
            # Use atom category term when available — preserves 13D/A vs 13D.
            schedule = (cat_m.group(1) if cat_m else schedule_label).replace("SC ", "")
            out.append((acc, schedule, href))
    return out


# ---------------------------------------------------------------------------
# Cover page parsing
# ---------------------------------------------------------------------------
# 13D cover pages are inconsistent across filers. We extract what we can with
# regex; missing fields stay NULL rather than gambling on bad parses.

_OWNERSHIP_PCT_RE = re.compile(
    r"PERCENT\s+OF\s+CLASS[\s\S]{0,200}?(\d+\.?\d*)\s*%", re.IGNORECASE
)
_AGGREGATE_AMOUNT_RE = re.compile(
    r"AGGREGATE\s+AMOUNT\s+BENEFICIALLY\s+OWNED[\s\S]{0,200}?([\d,]+)",
    re.IGNORECASE,
)
_EVENT_DATE_RE = re.compile(
    r"DATE\s+OF\s+EVENT[\s\S]{0,200}?(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
_ITEM4_RE = re.compile(
    r"ITEM\s+4\.?\s*PURPOSE\s+OF\s+TRANSACTION[\s\S]+?"
    r"(?=ITEM\s+5\.?|$)",
    re.IGNORECASE,
)


async def find_13d_body_url(session: aiohttp.ClientSession,
                            cik: int, accession: str) -> Optional[str]:
    """Locate the primary text/HTML body of a 13D filing."""
    nodash = accession.replace("-", "")
    index_url = f"{SEC_BASE}/Archives/edgar/data/{cik}/{nodash}/"
    html = await _get_text(session, index_url)
    candidates = re.findall(
        r'href="([^"]+\.(?:htm|html|txt))"', html, flags=re.IGNORECASE
    )
    if not candidates:
        return None
    # Prefer the largest non-index document — the cover page is usually the
    # primary doc (e.g. sched13d.htm). The directory listing already orders
    # the primary first, so first match is a reasonable default.
    for c in candidates:
        name = c.rsplit("/", 1)[-1].lower()
        if name in ("index.htm", "index.html", "0001.htm"):
            continue
        return f"{SEC_BASE}{c}" if c.startswith("/") else f"{index_url}{c}"
    return None


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s)


def parse_13d_body(text: str) -> dict:
    """Extract structured fields from a 13D/G body. Best-effort."""
    plain = _strip_html(text)
    plain = re.sub(r"\s+", " ", plain)

    pct: Optional[float] = None
    if (m := _OWNERSHIP_PCT_RE.search(plain)):
        try:
            pct = float(m.group(1))
        except ValueError:
            pct = None

    shares: Optional[int] = None
    if (m := _AGGREGATE_AMOUNT_RE.search(plain)):
        try:
            shares = int(m.group(1).replace(",", ""))
        except ValueError:
            shares = None

    event_date: Optional[str] = None
    if (m := _EVENT_DATE_RE.search(plain)):
        raw = m.group(1)
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                event_date = datetime.strptime(raw, fmt).date().isoformat()
                break
            except ValueError:
                continue

    intent: Optional[str] = None
    if (m := _ITEM4_RE.search(plain)):
        intent = m.group(0)[:4000]  # cap at 4KB

    return {
        "ownership_pct": pct,
        "shares_owned": shares,
        "event_date": event_date,
        "intent_summary": intent,
    }


# ---------------------------------------------------------------------------
# Discovering the filer CIK
# ---------------------------------------------------------------------------

async def find_filer_cik(session: aiohttp.ClientSession,
                         issuer_cik: int, accession: str) -> Tuple[Optional[int], Optional[str]]:
    """The submission's primary filer is in the index header.

    The index page (-index.htm) lists `Filed by` with each filer's CIK. The
    plaintext header at .../{accession}.txt is the most reliable parse source.
    """
    nodash = accession.replace("-", "")
    txt_url = f"{SEC_BASE}/Archives/edgar/data/{issuer_cik}/{nodash}/{accession}.txt"
    try:
        head = await _get_text(session, txt_url)
    except Exception:
        return None, None

    # Header has FILED BY: followed by COMPANY DATA blocks. The first block
    # whose CIK != issuer_cik is the filer.
    blocks = re.findall(
        r"COMPANY DATA:\s*COMPANY CONFORMED NAME:\s*([^\n]+)\s+CENTRAL INDEX KEY:\s*(\d+)",
        head,
        flags=re.IGNORECASE,
    )
    for name, cik_str in blocks:
        cik = int(cik_str)
        if cik != issuer_cik:
            return cik, name.strip()
    return None, None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist(accession: str, schedule: str, filer_cik: int, filer_name: str,
             entity_id: Optional[int], target_cik: int, target_ticker: Optional[str],
             target_name: str, filed_date: str, parsed: dict) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO activist_filings
                     (accession, schedule, filer_cik, filer_entity_id,
                      target_cik, target_ticker, target_name, filed_date,
                      event_date, ownership_pct, shares_owned,
                      intent_summary, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (accession, schedule, filer_cik, entity_id, target_cik,
                 target_ticker, target_name, filed_date,
                 parsed.get("event_date"), parsed.get("ownership_pct"),
                 parsed.get("shares_owned"), parsed.get("intent_summary"), now),
            )
            return True
        except Exception:
            # Likely UNIQUE conflict on accession — already ingested.
            return False


# ---------------------------------------------------------------------------
# Top-level ingest
# ---------------------------------------------------------------------------

async def ingest_issuer_13d(session: aiohttp.ClientSession,
                            issuer_cik: int,
                            issuer_ticker: Optional[str],
                            issuer_name: str) -> int:
    """Ingest recent 13D/G filings for one issuer. Returns rows inserted."""
    refs = await list_13d_for_issuer(session, issuer_cik)
    inserted = 0
    for accession, schedule, _href in refs:
        with get_conn() as conn:
            already = conn.execute(
                "SELECT 1 FROM activist_filings WHERE accession=?", (accession,)
            ).fetchone()
        if already:
            continue
        try:
            filer_cik, filer_name = await find_filer_cik(session, issuer_cik, accession)
            if not filer_cik or not filer_name:
                continue
            body_url = await find_13d_body_url(session, filer_cik, accession)
            parsed = {}
            if body_url:
                try:
                    body = await _get_text(session, body_url)
                    parsed = parse_13d_body(body)
                except Exception:
                    logger.exception("13d: body parse failed accession=%s", accession)

            entity = resolve_entity(filer_cik, filer_name, authority="13D")
            # Filed date approximated from accession (YY in middle): use today
            # as fetched_at fallback; for proper filed_date we'd need the atom
            # entry's <updated> timestamp. v1: use current date.
            filed_date = datetime.now(timezone.utc).date().isoformat()

            if _persist(accession=accession, schedule=schedule,
                        filer_cik=filer_cik, filer_name=filer_name,
                        entity_id=entity.entity_id, target_cik=issuer_cik,
                        target_ticker=issuer_ticker, target_name=issuer_name,
                        filed_date=filed_date, parsed=parsed):
                inserted += 1
        except Exception:
            logger.exception("13d: failed accession=%s issuer=%d", accession, issuer_cik)

    with get_conn() as conn:
        conn.execute(
            "UPDATE issuer_watchlist SET last_13d_check=? WHERE cik=?",
            (datetime.now(timezone.utc).isoformat(), issuer_cik),
        )
    return inserted


async def ingest_watchlist(limit: Optional[int] = None) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_runs (source, started_at, status) VALUES (?, ?, 'running')",
            ("edgar_13d", started),
        )
        run_id = cur.lastrowid
        rows = conn.execute(
            """SELECT cik, ticker, issuer_name FROM issuer_watchlist
                ORDER BY COALESCE(last_13d_check, '1970-01-01') ASC
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
                    total += await ingest_issuer_13d(
                        session, int(r["cik"]), r["ticker"], r["issuer_name"],
                    )
                except Exception as e:
                    logger.exception("13d: cik=%s failed", r["cik"])
                    error = f"{type(e).__name__}: {e}"
    finally:
        finished = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute(
                """UPDATE ingest_runs SET finished_at=?, status=?, n_new=?, error=?
                    WHERE id=?""",
                (finished, "error" if error else "ok", total, error, run_id),
            )
    return {"new_filings": total, "issuers_checked": len(rows), "error": error}
