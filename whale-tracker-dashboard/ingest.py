"""Background ingest loop.

Polls EDGAR every INGEST_INTERVAL seconds for the four feeds we care
about, and persists new filings into SQLite.

The loop is deliberately conservative:
  - Each pass fetches ~40 entries per feed (the Atom feed default)
  - We dedupe by accession before fetching the per-filing index
  - Each Form 4 needs an extra fetch (the XML) so we cap parallelism
"""

from __future__ import annotations

import asyncio
import logging
import os

import cik_ticker
import db
import edgar
import events
import filings8k
import filings13d
import form4

log = logging.getLogger("ingest")

INGEST_INTERVAL_S = int(os.environ.get("INGEST_INTERVAL_S", "300"))   # 5 min default
PER_FEED_COUNT    = int(os.environ.get("INGEST_FEED_COUNT", "40"))


async def run_once() -> dict[str, int]:
    """One ingest pass over all feeds. Returns counts inserted per feed."""
    results = {"form4": 0, "13d": 0, "13g": 0, "8k": 0}

    # Refresh CIK→ticker map (no-op if already current).
    try:
        await cik_ticker.ensure_loaded()
    except Exception as e:
        log.info("cik_ticker refresh skipped: %s", e)

    # Form 4 — insider transactions
    try:
        results["form4"] = await _ingest_form4()
    except Exception as e:
        log.exception("form4 ingest failed: %s", e)

    # SC 13D
    try:
        results["13d"] = await _ingest_13(form_type="SC 13D")
    except Exception as e:
        log.exception("13D ingest failed: %s", e)

    # SC 13G
    try:
        results["13g"] = await _ingest_13(form_type="SC 13G")
    except Exception as e:
        log.exception("13G ingest failed: %s", e)

    # 8-K
    try:
        results["8k"] = await _ingest_8k()
    except Exception as e:
        log.exception("8-K ingest failed: %s", e)

    if any(results.values()):
        events.broadcast("ingest", {"inserted": results, "counts": db.counts()})

    return results


async def loop_forever() -> None:
    log.info("ingest loop starting (interval=%ds)", INGEST_INTERVAL_S)
    db.init_db()
    while True:
        try:
            res = await run_once()
            log.info("ingest pass: %s", res)
        except Exception as e:
            log.exception("ingest pass crashed: %s", e)
        await asyncio.sleep(INGEST_INTERVAL_S)


# ───────────────────────────── Form 4 ─────────────────────────────

async def _ingest_form4() -> int:
    entries = await edgar.recent_atom("4", count=PER_FEED_COUNT)
    if not entries:
        return 0

    # Skip ones we've already stored.
    todo = [e for e in entries if e.get("accession") and not db.have_accession("insider_txn", e["accession"])]

    inserted_rows = 0
    # Fetch index.json then the XML doc for each. Limit concurrency.
    sem = asyncio.Semaphore(4)

    async def handle(entry: dict):
        nonlocal inserted_rows
        async with sem:
            try:
                rows = await _fetch_form4_rows(entry)
            except Exception as e:
                log.warning("form4 fetch failed for %s: %s", entry.get("accession"), e)
                return
            if rows:
                inserted_rows += db.upsert_insider_txns(rows)

    await asyncio.gather(*(handle(e) for e in todo))

    if entries:
        db.set_ingest_state("form4", entries[0].get("accession", ""))
    return inserted_rows


async def _fetch_form4_rows(entry: dict) -> list[dict]:
    cik = entry.get("filer_cik") or ""
    accession = entry.get("accession") or ""
    if not cik or not accession:
        return []
    idx = await edgar.fetch_filing_index(cik, accession)
    if not idx:
        return []
    doc = edgar.pick_doc(idx, suffixes=(".xml",))
    if not doc:
        return []
    doc_url = edgar.filing_primary_doc_url(cik, accession, doc)
    try:
        xml = await edgar.fetch(doc_url)
    except Exception as e:
        log.info("form4 doc fetch %s: %s", doc_url, e)
        return []
    return form4.parse_form4(xml, accession=accession, filed_at=entry.get("filed_at", ""), filing_url=doc_url)


# ───────────────────────────── SC 13D / 13G ─────────────────────────────

async def _ingest_13(form_type: str) -> int:
    entries = await edgar.recent_atom(form_type, count=PER_FEED_COUNT)
    if not entries:
        return 0
    inserted = 0
    sem = asyncio.Semaphore(4)

    async def handle(entry: dict):
        nonlocal inserted
        async with sem:
            accession = entry.get("accession") or ""
            if not accession or db.have_accession("activist_stake", accession):
                return
            try:
                row = await _build_13_row(entry, form_type)
            except Exception as e:
                log.warning("13 fetch failed for %s: %s", accession, e)
                return
            if row and db.upsert_activist_stake(row):
                inserted += 1

    await asyncio.gather(*(handle(e) for e in entries))
    return inserted


async def _build_13_row(entry: dict, form_type: str) -> dict | None:
    cik = entry.get("filer_cik") or ""
    accession = entry.get("accession") or ""
    if not cik or not accession:
        return None
    idx = await edgar.fetch_filing_index(cik, accession)
    if not idx:
        return None
    doc = edgar.pick_doc(idx, suffixes=(".htm", ".html", ".txt"))
    pct = None
    shares = None
    issuer_name_extracted = ""
    if doc:
        doc_url = edgar.filing_primary_doc_url(cik, accession, doc)
        try:
            body = await edgar.fetch(doc_url)
            parsed = filings13d.parse_13_filing(body)
            pct = parsed.get("pct_owned")
            shares = parsed.get("shares_owned")
            issuer_name_extracted = parsed.get("issuer_name_extracted", "")
        except Exception as e:
            log.info("13 doc fetch %s: %s", accession, e)
    else:
        doc_url = ""

    # The Atom entry's filer is the activist. Issuer name is sometimes on
    # the index.json (issuer block), otherwise we use what we extracted.
    primary = (idx or {}).get("primary_documents") or []
    issuer_name = ""
    issuer_ticker = ""
    issuer_cik = ""
    # `issuingEntity` lives on some filings' index.json:
    ie = (idx or {}).get("issuing_entity") or {}
    if ie:
        issuer_name = ie.get("name", "") or ""
        issuer_cik = ie.get("cik", "") or ""
        issuer_ticker = (ie.get("ticker") or "").upper()
    if not issuer_name:
        issuer_name = issuer_name_extracted

    # Fall back to the official CIK→ticker map if the filing didn't surface one.
    if not issuer_ticker and issuer_cik:
        issuer_ticker = cik_ticker.lookup_ticker(issuer_cik) or ""
        if not issuer_name:
            issuer_name = cik_ticker.lookup_name(issuer_cik) or ""

    return {
        "accession":     accession,
        "filed_at":      entry.get("filed_at", ""),
        "filer_name":    entry.get("filer_name", ""),
        "filer_cik":     entry.get("filer_cik", ""),
        "issuer_name":   issuer_name,
        "issuer_ticker": issuer_ticker or None,
        "issuer_cik":    issuer_cik,
        "pct_owned":     pct,
        "shares_owned":  shares,
        "filing_type":   form_type,
        "filing_url":    doc_url,
    }


# ───────────────────────────── 8-K ─────────────────────────────

async def _ingest_8k() -> int:
    entries = await edgar.recent_atom("8-K", count=PER_FEED_COUNT)
    if not entries:
        return 0
    inserted = 0
    for entry in entries:
        accession = entry.get("accession") or ""
        if not accession or db.have_accession("ma_event", accession):
            continue
        items = filings8k.parse_items_from_summary(entry.get("summary", ""))
        score = filings8k.score_8k(items, headline=entry.get("title", ""), body_excerpt=entry.get("summary", ""))
        if score < 2.0:
            continue
        cik = entry.get("filer_cik", "")
        ticker = cik_ticker.lookup_ticker(cik) if cik else None
        row = {
            "accession":     accession,
            "filed_at":      entry.get("filed_at", ""),
            "issuer_name":   entry.get("filer_name", ""),
            "issuer_ticker": ticker,
            "issuer_cik":    cik,
            "items":         ",".join(items),
            "headline":      entry.get("title", "")[:300],
            "ma_score":      score,
            "filing_url":    entry.get("link", ""),
        }
        if db.upsert_ma_event(row):
            inserted += 1
    return inserted


# ───────────────────────────── Backfill helper ─────────────────────────────

async def backfill_tickers() -> dict[str, int]:
    """Backfill issuer_ticker for activist/MA rows that don't have one.

    Useful after the first cik_tickers fetch or after rows were ingested
    before the map was available.
    """
    await cik_ticker.ensure_loaded()
    updated = {"activist_stake": 0, "ma_event": 0}
    with db.connect() as cx:
        a_rows = cx.execute(
            "SELECT accession, issuer_cik FROM activist_stake "
            "WHERE issuer_ticker IS NULL AND issuer_cik IS NOT NULL AND issuer_cik != ''"
        ).fetchall()
        for r in a_rows:
            t = cik_ticker.lookup_ticker(r["issuer_cik"])
            if t:
                cx.execute(
                    "UPDATE activist_stake SET issuer_ticker = ? WHERE accession = ?",
                    (t, r["accession"]),
                )
                updated["activist_stake"] += 1
        m_rows = cx.execute(
            "SELECT accession, issuer_cik FROM ma_event "
            "WHERE issuer_ticker IS NULL AND issuer_cik IS NOT NULL AND issuer_cik != ''"
        ).fetchall()
        for r in m_rows:
            t = cik_ticker.lookup_ticker(r["issuer_cik"])
            if t:
                cx.execute(
                    "UPDATE ma_event SET issuer_ticker = ? WHERE accession = ?",
                    (t, r["accession"]),
                )
                updated["ma_event"] += 1
    return updated
