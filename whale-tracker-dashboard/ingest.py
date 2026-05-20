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
import congress
import db
import edgar
import events
import filings8k
import filings13d
import form4
import form13f
import openfigi
import options_flow
import skill as skill_mod

log = logging.getLogger("ingest")

INGEST_INTERVAL_S = int(os.environ.get("INGEST_INTERVAL_S", "300"))   # 5 min default
PER_FEED_COUNT    = int(os.environ.get("INGEST_FEED_COUNT", "40"))
PER_PASS_13F_LIMIT = int(os.environ.get("INGEST_13F_LIMIT", "5"))     # cap 13F per pass — big XMLs

# Congress S3 buckets refresh ~daily; no need to pull every 5 minutes.
CONGRESS_INTERVAL_S = int(os.environ.get("CONGRESS_INTERVAL_S", "3600"))
_last_congress_run = 0.0

# Skill labeling runs on a slower beat than the filing ingest; price data is
# stable and each pass touches dozens of HTTP requests.
SKILL_INTERVAL_S    = int(os.environ.get("SKILL_INTERVAL_S", "1800"))   # 30 min default
SKILL_PER_PASS      = int(os.environ.get("SKILL_PER_PASS", "200"))
SKILL_HORIZON_DAYS  = int(os.environ.get("SKILL_HORIZON_DAYS", "30"))
_last_skill_run = 0.0

# Options flow + dark pool pull cadence. Real-time would mean WebSockets;
# poll-based at this cadence is good enough for the dashboard's update
# pattern. No-op when UNUSUAL_WHALES_API_KEY is unset.
OPTIONS_FLOW_INTERVAL_S = int(os.environ.get("OPTIONS_FLOW_INTERVAL_S", "120"))
OPTIONS_FLOW_LIMIT      = int(os.environ.get("OPTIONS_FLOW_LIMIT", "200"))
_last_options_run = 0.0

# OpenFIGI cadence — slow, only kicks in when there are unresolved CUSIPs.
OPENFIGI_INTERVAL_S = int(os.environ.get("OPENFIGI_INTERVAL_S", "1800"))
OPENFIGI_PER_PASS   = int(os.environ.get("OPENFIGI_PER_PASS", "500"))
_last_openfigi_run = 0.0


async def run_once() -> dict[str, int]:
    """One ingest pass over all feeds. Returns counts inserted per feed."""
    global _last_congress_run, _last_skill_run, _last_options_run, _last_openfigi_run
    results = {
        "form4": 0, "13d": 0, "13g": 0, "8k": 0,
        "13f_filings": 0, "13f_holdings": 0,
        "congress": 0, "skill_labeled": 0,
        "options_flow": 0, "dark_pool": 0,
        "cusips_resolved": 0,
    }

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

    # 13F-HR — fund quarterly holdings (capped per pass; big XMLs)
    try:
        nf, nh = await _ingest_13f()
        results["13f_filings"], results["13f_holdings"] = nf, nh
    except Exception as e:
        log.exception("13F ingest failed: %s", e)

    # Congress PTRs — pulled at a slower cadence
    loop = asyncio.get_event_loop()
    now = loop.time()
    if now - _last_congress_run >= CONGRESS_INTERVAL_S:
        try:
            results["congress"] = await _ingest_congress()
            _last_congress_run = now
        except Exception as e:
            log.exception("congress ingest failed: %s", e)

    # Options flow + dark pool — no-op without UNUSUAL_WHALES_API_KEY
    if options_flow.is_configured() and now - _last_options_run >= OPTIONS_FLOW_INTERVAL_S:
        try:
            results["options_flow"] = await _ingest_options_flow()
            results["dark_pool"] = await _ingest_dark_pool()
            _last_options_run = now
        except Exception as e:
            log.exception("options/dark-pool ingest failed: %s", e)

    # OpenFIGI CUSIP→ticker resolution for unresolved 13F holdings
    if now - _last_openfigi_run >= OPENFIGI_INTERVAL_S:
        try:
            results["cusips_resolved"] = await _resolve_unresolved_cusips()
            _last_openfigi_run = now
        except Exception as e:
            log.exception("openfigi resolve failed: %s", e)

    # Bayesian skill labeling — also slow cadence
    if now - _last_skill_run >= SKILL_INTERVAL_S:
        try:
            res = await skill_mod.run_pass(horizon_days=SKILL_HORIZON_DAYS, limit=SKILL_PER_PASS)
            results["skill_labeled"] = res.get("labeled", 0)
            _last_skill_run = now
        except Exception as e:
            log.exception("skill labeling failed: %s", e)

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


# ───────────────────────────── 13F-HR ─────────────────────────────

async def _ingest_13f() -> tuple[int, int]:
    """Pull recent 13F-HR filings, parse the INFORMATION TABLE, persist.

    Each filing's XML can be megabytes, so we cap concurrency and the
    number processed per pass. Over multiple passes, the full Atom feed
    gets ingested.
    """
    entries = await edgar.recent_atom("13F-HR", count=PER_FEED_COUNT)
    if not entries:
        return (0, 0)

    todo = [e for e in entries
            if e.get("accession") and not db.have_accession("fund_filing", e["accession"])]
    todo = todo[:PER_PASS_13F_LIMIT]

    filings_inserted = 0
    holdings_inserted = 0
    sem = asyncio.Semaphore(2)

    async def handle(entry: dict):
        nonlocal filings_inserted, holdings_inserted
        async with sem:
            try:
                nf, nh = await _process_13f(entry)
            except Exception as e:
                log.warning("13F process failed for %s: %s", entry.get("accession"), e)
                return
            filings_inserted += nf
            holdings_inserted += nh

    await asyncio.gather(*(handle(e) for e in todo))
    return (filings_inserted, holdings_inserted)


async def _process_13f(entry: dict) -> tuple[int, int]:
    cik = entry.get("filer_cik") or ""
    accession = entry.get("accession") or ""
    if not cik or not accession:
        return (0, 0)

    idx = await edgar.fetch_filing_index(cik, accession)
    if not idx:
        return (0, 0)

    # 13F filings have two XML docs: a primary doc (metadata) and an
    # INFORMATION TABLE. Filenames vary; we identify by suffix and content.
    items = (idx or {}).get("directory", {}).get("item", [])
    xml_names = [it.get("name", "") for it in items if it.get("name", "").lower().endswith(".xml")]
    if not xml_names:
        return (0, 0)

    primary_xml = ""
    info_xml = ""

    # Heuristic: the information table file usually has "infotable" or
    # "form13fInfoTable" in its name. The primary doc is the remaining XML.
    info_candidates = [n for n in xml_names if "info" in n.lower()]
    other_candidates = [n for n in xml_names if n not in info_candidates]

    for name in info_candidates:
        url = edgar.filing_primary_doc_url(cik, accession, name)
        try:
            body = await edgar.fetch(url)
        except Exception as e:
            log.info("13F info fetch %s: %s", accession, e)
            continue
        if "<infoTable" in body or "informationTable" in body:
            info_xml = body
            break

    for name in other_candidates:
        url = edgar.filing_primary_doc_url(cik, accession, name)
        try:
            body = await edgar.fetch(url)
        except Exception as e:
            log.info("13F primary fetch %s: %s", accession, e)
            continue
        if "periodOfReport" in body or "filingManager" in body:
            primary_xml = body
            break

    period = form13f.extract_period_of_report(primary_xml) if primary_xml else ""
    fund_name = form13f.extract_fund_name(primary_xml) if primary_xml else (entry.get("filer_name") or "")

    holdings = form13f.parse_information_table(
        info_xml, accession=accession, fund_cik=cik, period_of_report=period
    ) if info_xml else []

    if not holdings:
        return (0, 0)

    # Resolve issuer_ticker for each holding. Strategy is:
    #   1. cusip → ticker lookup against the local cache (OpenFIGI-fed)
    #   2. fallback to issuer-name normalised match against company_tickers.json
    # Step 1 hits any CUSIP we've previously resolved; step 2 catches everything
    # else with big-cap recall. Unresolved CUSIPs accumulate and get sent to
    # OpenFIGI on the next ingest pass.
    for h in holdings:
        if not h.get("issuer_ticker"):
            cusip = h.get("cusip")
            if cusip:
                t = db.lookup_cusip_ticker(cusip)
                if t:
                    h["issuer_ticker"] = t
            if not h.get("issuer_ticker") and h.get("issuer_name"):
                t = cik_ticker.resolve_ticker_from_name(h["issuer_name"])
                if t:
                    h["issuer_ticker"] = t

    total_value = sum((h["value"] or 0) for h in holdings)

    filing_row = {
        "accession":        accession,
        "filed_at":         entry.get("filed_at", ""),
        "period_of_report": period or None,
        "fund_cik":         cik,
        "fund_name":        fund_name or entry.get("filer_name", ""),
        "total_value":      total_value,
        "holding_count":    len(holdings),
        "filing_url":       edgar.filing_primary_doc_url(cik, accession, info_candidates[0]) if info_candidates else "",
    }

    inserted_filing = 1 if db.upsert_fund_filing(filing_row) else 0
    inserted_holdings = db.upsert_fund_holdings(holdings)
    return (inserted_filing, inserted_holdings)


# ───────────────────────────── Congress PTRs ─────────────────────────────

async def _ingest_congress() -> int:
    inserted = 0
    try:
        house = await congress.fetch_house()
        inserted += db.upsert_congress_trades(house)
    except Exception as e:
        log.warning("congress house fetch failed: %s", e)
    try:
        senate = await congress.fetch_senate()
        inserted += db.upsert_congress_trades(senate)
    except Exception as e:
        log.warning("congress senate fetch failed: %s", e)
    return inserted


# ───────────────────────────── Backfill helper ─────────────────────────────

# ───────────────────────────── Options flow / dark pool ─────────────────────────────

async def _ingest_options_flow() -> int:
    rows = await options_flow.fetch_flow_alerts(limit=OPTIONS_FLOW_LIMIT)
    rows = [r for r in rows if r.get("alert_id") and r.get("ticker")]
    return db.upsert_options_flow(rows) if rows else 0


async def _ingest_dark_pool() -> int:
    rows = await options_flow.fetch_dark_pool_prints(limit=OPTIONS_FLOW_LIMIT)
    rows = [r for r in rows if r.get("print_id") and r.get("ticker")]
    return db.upsert_dark_pool(rows) if rows else 0


# ───────────────────────────── OpenFIGI ─────────────────────────────

async def _resolve_unresolved_cusips() -> int:
    cusips = db.unresolved_cusips(limit=OPENFIGI_PER_PASS)
    if not cusips:
        return 0
    resolved = await openfigi.resolve_and_persist(cusips)
    if resolved:
        # Backfill the tickers we just learned into fund_holding.
        n = await _backfill_cusip_tickers_into_holdings(cusips)
        log.info("openfigi: resolved %d, backfilled %d fund_holding rows", resolved, n)
    return resolved


async def _backfill_cusip_tickers_into_holdings(cusips: list[str]) -> int:
    if not cusips:
        return 0
    updated = 0
    with db.connect() as cx:
        for c in cusips:
            row = cx.execute(
                "SELECT ticker FROM cusip_ticker WHERE cusip = ? LIMIT 1",
                (c,),
            ).fetchone()
            if not row:
                continue
            cur = cx.execute(
                "UPDATE fund_holding SET issuer_ticker = ? "
                "WHERE cusip = ? AND issuer_ticker IS NULL",
                (row["ticker"], c),
            )
            updated += cur.rowcount
    return updated


async def backfill_filings(form_types: list[str], *, start_date: str, end_date: str,
                           max_per_form: int = 500) -> dict[str, int]:
    """Pull historical filings via EDGAR full-text search and route them
    through the same per-form handlers used by the live ingest.

    This is what warms the skill model on day one — without it we'd only
    have the rolling 40-entry Atom feed and have to wait months for the
    posteriors to converge.

    `form_types` accepts: '4', 'SC 13D', 'SC 13G', '8-K', '13F-HR'.
    Stops once `max_per_form` filings are processed per form.
    """
    await cik_ticker.ensure_loaded()
    results: dict[str, int] = {}

    for form in form_types:
        inserted = 0
        offset = 0
        page_size = 100
        while inserted < max_per_form:
            try:
                hits = await edgar.search_filings(
                    form, start_date=start_date, end_date=end_date,
                    offset=offset, size=page_size,
                )
            except Exception as e:
                log.warning("backfill %s search failed at offset %d: %s", form, offset, e)
                break
            if not hits:
                break
            # Process this page via the same logic as live ingest.
            try:
                if form == "4":
                    todo = [e for e in hits if e.get("accession")
                            and not db.have_accession("insider_txn", e["accession"])]
                    sem = asyncio.Semaphore(4)
                    async def hf(e):
                        async with sem:
                            rows = await _fetch_form4_rows(e)
                            if rows:
                                return db.upsert_insider_txns(rows)
                            return 0
                    counts = await asyncio.gather(*(hf(e) for e in todo))
                    inserted += sum(counts)
                elif form in ("SC 13D", "SC 13G"):
                    sem = asyncio.Semaphore(4)
                    async def h13(e):
                        async with sem:
                            if not e.get("accession") or db.have_accession("activist_stake", e["accession"]):
                                return 0
                            row = await _build_13_row(e, form)
                            return 1 if (row and db.upsert_activist_stake(row)) else 0
                    counts = await asyncio.gather(*(h13(e) for e in hits))
                    inserted += sum(counts)
                elif form == "8-K":
                    for e in hits:
                        accession = e.get("accession") or ""
                        if not accession or db.have_accession("ma_event", accession):
                            continue
                        items = filings8k.parse_items_from_summary(e.get("summary", ""))
                        score = filings8k.score_8k(
                            items,
                            headline=e.get("title", ""),
                            body_excerpt=e.get("summary", ""),
                        )
                        if score < 2.0:
                            continue
                        cik = e.get("filer_cik", "")
                        ticker = cik_ticker.lookup_ticker(cik) if cik else None
                        row = {
                            "accession":     accession,
                            "filed_at":      e.get("filed_at", ""),
                            "issuer_name":   e.get("filer_name", ""),
                            "issuer_ticker": ticker,
                            "issuer_cik":    cik,
                            "items":         ",".join(items),
                            "headline":      e.get("title", "")[:300],
                            "ma_score":      score,
                            "filing_url":    e.get("link", ""),
                        }
                        if db.upsert_ma_event(row):
                            inserted += 1
                elif form == "13F-HR":
                    todo = [e for e in hits if e.get("accession")
                            and not db.have_accession("fund_filing", e["accession"])]
                    sem = asyncio.Semaphore(2)
                    async def h13f(e):
                        async with sem:
                            try:
                                nf, _ = await _process_13f(e)
                                return nf
                            except Exception:
                                return 0
                    counts = await asyncio.gather(*(h13f(e) for e in todo))
                    inserted += sum(counts)
                else:
                    log.info("backfill: skipping unsupported form %r", form)
                    break
            except Exception as e:
                log.exception("backfill %s page-process failed: %s", form, e)
                break

            if len(hits) < page_size:
                break
            offset += page_size

        results[form] = inserted

    return results


async def backfill_tickers() -> dict[str, int]:
    """Backfill issuer_ticker for activist/MA/13F-holding rows that don't have one.

    Useful after the first cik_tickers fetch or after rows were ingested
    before the map was available.
    """
    await cik_ticker.ensure_loaded()
    updated = {"activist_stake": 0, "ma_event": 0, "fund_holding": 0}
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
        # 13F holdings: try the CUSIP→ticker cache first (populated by OpenFIGI),
        # then fall back to the unambiguous issuer-name index.
        h_rows = cx.execute(
            "SELECT accession, line_no, cusip, issuer_name FROM fund_holding "
            "WHERE issuer_ticker IS NULL"
        ).fetchall()
        for r in h_rows:
            t = None
            if r["cusip"]:
                t = db.lookup_cusip_ticker(r["cusip"])
            if not t and r["issuer_name"]:
                t = cik_ticker.resolve_ticker_from_name(r["issuer_name"])
            if t:
                cx.execute(
                    "UPDATE fund_holding SET issuer_ticker = ? "
                    "WHERE accession = ? AND line_no = ?",
                    (t, r["accession"], r["line_no"]),
                )
                updated["fund_holding"] += 1
    return updated
