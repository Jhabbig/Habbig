from __future__ import annotations
"""Whale Dashboard FastAPI server.

Mirrors the conventions of midterm-dashboard/backend/main.py: FastAPI with
SQLite storage, gateway SSO via x-gateway-secret, daemon-thread workers for
periodic ingest, no APScheduler.

Endpoints:
    GET  /health
    GET  /                                — basic JSON, also serves frontend
    GET  /api/whales
    GET  /api/whale/{slug}
    GET  /api/whale/{slug}/deltas?q=YYYY-MM-DD
    GET  /api/ticker/{ticker}
    GET  /api/ticker/{ticker}/insider
    GET  /api/feed
    GET  /api/activist                    — recent 13D/13G filings
    GET  /api/cluster-buys                — Form 4 cluster signal
    GET  /api/correlations                — Polymarket cross-links
    GET  /api/runs                        — recent ingest runs (admin)
    GET  /api/consensus, /api/crowdedness, /api/cot
    GET/POST/DELETE /api/watchlist        — per-user ticker/whale watchlist
    GET/POST/DELETE /api/alerts           — per-user alert rules
    WS   /ws/feed                          — real-time row-fanout
    POST /api/admin/ingest/{kind}         — kinds: 13f, form4, 13d, cusip,
                                             openfigi, polymarket, cot,
                                             consensus, alerts
"""

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import alerts as alerts_module
import ws_feed
from analysis.consensus import recompute_consensus
from analysis.entity_seed import load_seed_into_db
from analysis.intent_classifier import backfill_existing as backfill_intent
from auth import require_auth, require_tier
from correlation.polymarket_link import sweep_recent_filings, update_followup_prices
from data_sources.cftc_cot import ingest as ingest_cot
from data_sources.cusip_seeder import run_full_seed as ingest_cusip, refresh_issuer_watchlist
from data_sources.edgar_13d import ingest_watchlist as ingest_13d
from data_sources.edgar_13f import ingest_seeded_entities as ingest_13f
from data_sources.edgar_form4 import ingest_watchlist as ingest_form4
from database import get_conn, init_db


# ---------------------------------------------------------------------------
# Logging + config
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("whale-dashboard")

PORT = int(os.getenv("PORT", "8053"))
EDGAR_13F_INTERVAL_S   = int(os.getenv("EDGAR_13F_INTERVAL_S",  str(6 * 3600)))   # 6h
EDGAR_FORM4_INTERVAL_S = int(os.getenv("EDGAR_FORM4_INTERVAL_S", str(30 * 60)))    # 30m
EDGAR_13D_INTERVAL_S   = int(os.getenv("EDGAR_13D_INTERVAL_S",  str(60 * 60)))    # 1h
POLYMARKET_INTERVAL_S  = int(os.getenv("POLYMARKET_INTERVAL_S", str(15 * 60)))    # 15m
CFTC_COT_INTERVAL_S    = int(os.getenv("CFTC_COT_INTERVAL_S",   str(24 * 3600)))   # 24h
ALERTS_INTERVAL_S      = int(os.getenv("ALERTS_INTERVAL_S",     str(5 * 60)))      # 5m

# Per-cycle work caps. Form 4 / 13D have many issuers; we round-robin across
# the watchlist by `last_*_check` and only process N per cycle so a single run
# completes in a reasonable time.
FORM4_PER_CYCLE = int(os.getenv("FORM4_PER_CYCLE", "150"))
D13_PER_CYCLE   = int(os.getenv("D13_PER_CYCLE",   "150"))
# Cap how many CUSIPs we send to OpenFIGI per post-13F cycle. Without an
# API key OpenFIGI is rate-limited to 25 jobs/min in batches of 10 — 200
# CUSIPs takes ~8 minutes. With a key, 500 CUSIPs takes ~15s.
CUSIP_RESOLVE_PER_CYCLE = int(os.getenv("CUSIP_RESOLVE_PER_CYCLE", "200"))

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


# ---------------------------------------------------------------------------
# Workers (daemon threads — same pattern as polymarket_weather_dashboard)
# ---------------------------------------------------------------------------

def _periodic(name: str, fn, interval_s: int) -> None:
    while True:
        try:
            logger.info("%s: starting scheduled run", name)
            result = asyncio.run(fn())
            logger.info("%s: done %s", name, result)
        except Exception:
            logger.exception("%s: loop crashed", name)
        time.sleep(interval_s)


async def _form4_cycle() -> dict:
    return await ingest_form4(limit=FORM4_PER_CYCLE)


async def _13d_cycle() -> dict:
    return await ingest_13d(limit=D13_PER_CYCLE)


async def _polymarket_cycle() -> dict:
    sweep = await sweep_recent_filings(hours_back=72)
    follow = await update_followup_prices()
    return {**sweep, **follow}


async def _alerts_cycle() -> dict:
    return await alerts_module.run_dispatcher(window_hours=24)


async def _post_13f_cycle() -> dict:
    """Run the 13F ingest then the analytics pipeline that depends on it:
    CUSIP→ticker resolution (OpenFIGI + fuzzy fallback), consensus rebuild,
    intent classifier backfill (in case prior 13D rows arrived without
    classification)."""
    r = await ingest_13f()
    # Resolve any new (cusip, issuer_name) pairs the ingest wrote. OpenFIGI
    # is rate-capped, so we only ask it for at most CUSIP_RESOLVE_PER_CYCLE
    # cusips per cycle and let the next cycle pick up the rest.
    try:
        cusip = await ingest_cusip(use_openfigi=True,
                                   openfigi_max=CUSIP_RESOLVE_PER_CYCLE)
    except Exception as e:
        logger.warning("post_13f: cusip resolution failed — %s: %s",
                       type(e).__name__, e)
        cusip = {"error": str(e)}
    consensus_n = recompute_consensus()
    intent_n = backfill_intent()
    return {**r, "cusip": cusip,
            "consensus_rows": consensus_n, "intent_classified": intent_n}


def _start_workers() -> None:
    if os.getenv("WHALE_NO_WORKERS") == "1":
        logger.info("workers: disabled via WHALE_NO_WORKERS")
        return
    workers = [
        ("edgar_13f",     _post_13f_cycle,      EDGAR_13F_INTERVAL_S),
        ("edgar_form4",   _form4_cycle,         EDGAR_FORM4_INTERVAL_S),
        ("edgar_13d",     _13d_cycle,           EDGAR_13D_INTERVAL_S),
        ("polymarket",    _polymarket_cycle,    POLYMARKET_INTERVAL_S),
        ("cftc_cot",      ingest_cot,           CFTC_COT_INTERVAL_S),
        ("alerts",        _alerts_cycle,        ALERTS_INTERVAL_S),
    ]
    for name, fn, interval in workers:
        t = threading.Thread(target=_periodic, args=(name, fn, interval),
                             name=name, daemon=True)
        t.start()
        logger.info("workers: started %s (interval=%ds)", name, interval)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_seed_into_db()

    # On first boot, the issuer_watchlist is empty, which means Form 4 / 13D
    # workers have nothing to do. Kick off a one-shot bootstrap in the
    # background so the workers have data to process on their first cycle.
    if os.getenv("WHALE_NO_WORKERS") != "1":
        async def _bootstrap() -> None:
            try:
                with get_conn() as conn:
                    n = conn.execute(
                        "SELECT COUNT(*) AS n FROM issuer_watchlist"
                    ).fetchone()["n"]
                if n == 0:
                    logger.info("bootstrap: seeding issuer_watchlist")
                    await refresh_issuer_watchlist()
            except Exception:
                logger.exception("bootstrap: failed")
        asyncio.create_task(_bootstrap())

    _start_workers()

    # Start the WS fanout poller as an asyncio task (not a thread — it needs
    # to broadcast into the same event loop as the WebSocket handlers).
    if os.getenv("WHALE_NO_WORKERS") != "1":
        asyncio.create_task(ws_feed.fanout_loop())

    yield


app = FastAPI(title="Whale Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    # Mount /static for css/js. Index is served via the / route below so the
    # gateway healthcheck (GET /) returns HTML rather than JSON.
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "whale-dashboard"}


@app.get("/")
async def root():
    """Serve the SPA. The gateway healthcheck hits this; serving a small
    HTML file is fine — the healthcheck only cares about a 200."""
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"service": "whale-dashboard", "status": "ok"})


@app.get("/api/whales")
async def list_whales(request: Request,
                      include_unverified: bool = False) -> JSONResponse:
    """List tracked entities. Defaults to curated/high-confidence only;
    pass ?include_unverified=true to surface auto-created 13D filers
    (useful for the admin "manual review and merge" flow)."""
    await require_auth(request)
    # Auto-created 13D filers get description="Auto-created — review and
    # merge if duplicate." We hide those by default — the public dashboard
    # should show the 17 curated mega-funds, not 500+ small activists.
    where_clause = ("WHERE COALESCE(e.description, '') NOT LIKE 'Auto-created%'"
                    if not include_unverified else "")
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT e.id, e.slug, e.parent_name, e.entity_type, e.description,
                      e.last_aum_usd,
                      (SELECT COUNT(*) FROM cik_map WHERE entity_id=e.id) AS n_ciks,
                      (SELECT MAX(quarter_end) FROM filings_13f f
                         JOIN cik_map c ON c.cik=f.cik
                        WHERE c.entity_id=e.id) AS latest_quarter,
                      (SELECT SUM(value_usd) FROM holdings h
                         JOIN filings_13f f ON f.id=h.filing_id
                         JOIN cik_map c ON c.cik=f.cik
                        WHERE c.entity_id=e.id
                          AND f.quarter_end=(SELECT MAX(quarter_end) FROM filings_13f f2
                                              JOIN cik_map c2 ON c2.cik=f2.cik
                                              WHERE c2.entity_id=e.id)
                      ) AS latest_book_usd
                 FROM entities e
                 {where_clause}
                ORDER BY e.last_aum_usd DESC NULLS LAST, e.parent_name"""
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/whale/{slug}")
async def whale_detail(slug: str, request: Request) -> JSONResponse:
    await require_auth(request)
    with get_conn() as conn:
        ent = conn.execute(
            "SELECT * FROM entities WHERE slug=?", (slug,)
        ).fetchone()
        if not ent:
            raise HTTPException(404, "Whale not found")

        latest = conn.execute(
            """SELECT MAX(quarter_end) AS q
                 FROM filings_13f f JOIN cik_map c ON c.cik=f.cik
                WHERE c.entity_id=?""",
            (ent["id"],),
        ).fetchone()
        latest_q = latest["q"] if latest else None

        positions = []
        if latest_q:
            positions = [dict(r) for r in conn.execute(
                """SELECT h.cusip, h.ticker, h.issuer_name, h.title_of_class,
                          SUM(h.shares) AS shares, SUM(h.value_usd) AS value_usd,
                          h.put_call
                     FROM holdings h
                     JOIN filings_13f f ON f.id=h.filing_id
                     JOIN cik_map c ON c.cik=f.cik
                    WHERE c.entity_id=? AND f.quarter_end=?
                    GROUP BY h.cusip, h.put_call
                    ORDER BY value_usd DESC
                    LIMIT 100""",
                (ent["id"], latest_q),
            ).fetchall()]

        ciks = [dict(r) for r in conn.execute(
            "SELECT cik, sub_name, filing_authority FROM cik_map WHERE entity_id=?",
            (ent["id"],),
        ).fetchall()]

        recent_13d = [dict(r) for r in conn.execute(
            """SELECT accession, schedule, target_ticker, target_name,
                      filed_date, ownership_pct
                 FROM activist_filings
                WHERE filer_entity_id=?
                ORDER BY filed_date DESC
                LIMIT 25""",
            (ent["id"],),
        ).fetchall()]

    return JSONResponse({
        "entity": dict(ent),
        "ciks": ciks,
        "latest_quarter": latest_q,
        "top_positions": positions,
        "recent_13d": recent_13d,
    })


@app.get("/api/whale/{slug}/deltas")
async def whale_deltas(slug: str, request: Request,
                       q: Optional[str] = None) -> JSONResponse:
    await require_auth(request)
    with get_conn() as conn:
        ent = conn.execute("SELECT id FROM entities WHERE slug=?", (slug,)).fetchone()
        if not ent:
            raise HTTPException(404, "Whale not found")
        if not q:
            row = conn.execute(
                "SELECT MAX(quarter_end) AS q FROM holdings_delta WHERE entity_id=?",
                (ent["id"],),
            ).fetchone()
            q = row["q"] if row and row["q"] else None
        if not q:
            return JSONResponse({"quarter_end": None, "deltas": []})
        rows = conn.execute(
            """SELECT * FROM holdings_delta
                WHERE entity_id=? AND quarter_end=?
                ORDER BY ABS(COALESCE(delta_value_usd, 0)) DESC
                LIMIT 200""",
            (ent["id"], q),
        ).fetchall()
    return JSONResponse({"quarter_end": q, "deltas": [dict(r) for r in rows]})


@app.get("/api/ticker/{ticker}")
async def ticker_detail(ticker: str, request: Request) -> JSONResponse:
    await require_auth(request)
    ticker = ticker.upper()
    with get_conn() as conn:
        latest_q_row = conn.execute(
            "SELECT MAX(quarter_end) AS q FROM filings_13f"
        ).fetchone()
        latest_q = latest_q_row["q"] if latest_q_row else None
        if not latest_q:
            return JSONResponse({"ticker": ticker, "latest_quarter": None,
                                 "holders": [], "moves": []})

        holders = [dict(r) for r in conn.execute(
            """SELECT e.slug, e.parent_name, e.entity_type,
                      SUM(h.shares) AS shares, SUM(h.value_usd) AS value_usd
                 FROM holdings h
                 JOIN filings_13f f ON f.id=h.filing_id
                 JOIN cik_map c ON c.cik=f.cik
                 JOIN entities e ON e.id=c.entity_id
                WHERE h.ticker=? AND f.quarter_end=?
                  AND h.put_call IS NULL
                GROUP BY e.id
                ORDER BY value_usd DESC
                LIMIT 50""",
            (ticker, latest_q),
        ).fetchall()]

        moves = [dict(r) for r in conn.execute(
            """SELECT e.slug, e.parent_name, hd.action, hd.delta_shares,
                      hd.delta_pct, hd.delta_value_usd
                 FROM holdings_delta hd
                 JOIN entities e ON e.id=hd.entity_id
                WHERE hd.ticker=? AND hd.quarter_end=?
                ORDER BY ABS(COALESCE(hd.delta_value_usd,0)) DESC
                LIMIT 50""",
            (ticker, latest_q),
        ).fetchall()]

    return JSONResponse({
        "ticker": ticker,
        "latest_quarter": latest_q,
        "holders": holders,
        "moves": moves,
    })


@app.get("/api/ticker/{ticker}/insider")
async def ticker_insider(ticker: str, request: Request,
                         days: int = 90) -> JSONResponse:
    """Recent insider transactions for a ticker. Filtered to P/S/A codes."""
    await require_auth(request)
    ticker = ticker.upper()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT insider_name, insider_role, txn_date, txn_code,
                      shares, price, value_usd, post_holdings
                 FROM insider_txns
                WHERE issuer_ticker=?
                  AND txn_date >= date('now', ?)
                  AND txn_code IN ('P','S','A')
                ORDER BY txn_date DESC
                LIMIT 200""",
            (ticker, f"-{int(days)} days"),
        ).fetchall()
    return JSONResponse({"ticker": ticker, "txns": [dict(r) for r in rows]})


@app.get("/api/feed")
async def feed(request: Request, limit: int = 50) -> JSONResponse:
    await require_auth(request)
    limit = max(1, min(limit, 200))
    with get_conn() as conn:
        # Union of recent filings across all sources, newest first.
        rows = conn.execute(
            """SELECT 'edgar_13f' AS source, f.accession, f.form_type AS kind,
                      f.filed_date, e.parent_name AS filer,
                      NULL AS target_ticker, NULL AS target_name,
                      f.total_value_usd AS value_usd
                 FROM filings_13f f
                 JOIN cik_map c ON c.cik=f.cik
                 JOIN entities e ON e.id=c.entity_id
              UNION ALL
               SELECT 'edgar_13d' AS source, accession, schedule AS kind,
                      filed_date, COALESCE(
                        (SELECT parent_name FROM entities WHERE id=filer_entity_id),
                        '(unmapped filer)') AS filer,
                      target_ticker, target_name,
                      ownership_pct AS value_usd
                 FROM activist_filings
              UNION ALL
               SELECT 'edgar_form4' AS source, accession, txn_code AS kind,
                      txn_date AS filed_date, insider_name AS filer,
                      issuer_ticker AS target_ticker, issuer_name AS target_name,
                      value_usd
                 FROM insider_txns
              ORDER BY filed_date DESC
              LIMIT ?""",
            (limit,),
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/activist")
async def activist_feed(request: Request, limit: int = 100) -> JSONResponse:
    await require_auth(request)
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT a.accession, a.schedule, a.filed_date, a.event_date,
                      a.target_ticker, a.target_name, a.ownership_pct,
                      a.shares_owned,
                      e.slug AS filer_slug, e.parent_name AS filer_name,
                      e.entity_type AS filer_type,
                      SUBSTR(a.intent_summary, 1, 280) AS intent_excerpt
                 FROM activist_filings a
                 LEFT JOIN entities e ON e.id=a.filer_entity_id
                ORDER BY a.filed_date DESC, a.id DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/cluster-buys")
async def cluster_buys(request: Request, days: int = 14,
                       min_insiders: int = 3) -> JSONResponse:
    """Tickers where >=N distinct insiders bought in the last N days.

    Strong signal — multiple insiders independently transacting the same way
    in a tight window is rarely coincidence.
    """
    await require_auth(request)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT issuer_ticker, issuer_name,
                      COUNT(DISTINCT insider_name) AS n_insiders,
                      SUM(shares) AS total_shares,
                      SUM(value_usd) AS total_value,
                      MIN(txn_date) AS first_txn,
                      MAX(txn_date) AS last_txn
                 FROM insider_txns
                WHERE txn_code='P'
                  AND txn_date >= date('now', ?)
                  AND issuer_ticker IS NOT NULL
                GROUP BY issuer_ticker
               HAVING n_insiders >= ?
                ORDER BY n_insiders DESC, total_value DESC
                LIMIT 100""",
            (f"-{int(days)} days", int(min_insiders)),
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/correlations")
async def correlations(request: Request, limit: int = 100) -> JSONResponse:
    """Polymarket cross-links, sorted by absolute price move."""
    await require_auth(request)
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT mc.*,
                      CASE mc.source_table
                        WHEN 'activist_filings' THEN
                          (SELECT target_ticker || ' / ' || target_name
                             FROM activist_filings WHERE id=mc.source_id)
                        WHEN 'insider_txns' THEN
                          (SELECT issuer_ticker || ' / ' || insider_name
                             FROM insider_txns WHERE id=mc.source_id)
                      END AS source_label
                 FROM market_correlation mc
                ORDER BY ABS(COALESCE(mc.edge_bps, 0)) DESC, mc.recorded_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/runs")
async def runs(request: Request, limit: int = 50) -> JSONResponse:
    """Recent ingest runs — visible to admin only."""
    await require_tier(request, "admin")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.post("/api/admin/ingest/{kind}")
async def admin_ingest(kind: str, request: Request) -> JSONResponse:
    await require_tier(request, "admin")
    if kind == "13f":
        return JSONResponse(await _post_13f_cycle())
    if kind == "form4":
        return JSONResponse(await ingest_form4(limit=FORM4_PER_CYCLE))
    if kind == "13d":
        return JSONResponse(await ingest_13d(limit=D13_PER_CYCLE))
    if kind == "cusip":
        return JSONResponse(await ingest_cusip())
    if kind == "openfigi":
        # Run only the OpenFIGI pass — useful for upgrading existing
        # fuzzy_name mappings without re-fetching company_tickers.json.
        from data_sources.openfigi import resolve_unmapped_cusips_via_openfigi
        max_cusips = int(request.query_params.get("max", "500"))
        return JSONResponse(await resolve_unmapped_cusips_via_openfigi(
            max_cusips=max_cusips
        ))
    if kind == "13d_backfill":
        # Re-fetch body for activist_filings rows that have NULL
        # intent_summary (these were ingested before the body-URL fix).
        from data_sources.edgar_13d import backfill_bodies
        limit = int(request.query_params.get("limit", "50"))
        return JSONResponse(await backfill_bodies(limit=limit))
    if kind == "polymarket":
        return JSONResponse(await _polymarket_cycle())
    if kind == "cot":
        return JSONResponse(await ingest_cot())
    if kind == "consensus":
        return JSONResponse({"rows": recompute_consensus(),
                             "intent_classified": backfill_intent()})
    if kind == "alerts":
        return JSONResponse(await _alerts_cycle())
    raise HTTPException(400, f"Unknown ingest kind: {kind}")


# ---------------------------------------------------------------------------
# v2 endpoints — consensus, COT, watchlist, alerts, WS
# ---------------------------------------------------------------------------

@app.get("/api/consensus")
async def consensus(request: Request,
                    quarter: Optional[str] = None,
                    min_whales: int = 5,
                    direction: str = "any",
                    limit: int = 100) -> JSONResponse:
    """Smart-money consensus snapshot.

    direction: "long" = positive consensus (accumulation),
               "short" = negative consensus (distribution),
               "any"   = sorted by abs(consensus_score)
    """
    await require_auth(request)
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        if not quarter:
            row = conn.execute(
                "SELECT MAX(quarter_end) AS q FROM consensus_snapshots"
            ).fetchone()
            quarter = row["q"] if row and row["q"] else None
        if not quarter:
            return JSONResponse({"quarter_end": None, "rows": []})

        order = {
            "long":  "consensus_score DESC",
            "short": "consensus_score ASC",
            "any":   "ABS(consensus_score) DESC",
        }.get(direction, "ABS(consensus_score) DESC")

        rows = conn.execute(
            f"""SELECT * FROM consensus_snapshots
                 WHERE quarter_end=? AND n_whales_long >= ?
                 ORDER BY {order}
                 LIMIT ?""",
            (quarter, min_whales, limit),
        ).fetchall()
    return JSONResponse({"quarter_end": quarter,
                         "rows": [dict(r) for r in rows]})


@app.get("/api/crowdedness")
async def crowdedness(request: Request,
                      quarter: Optional[str] = None,
                      limit: int = 100) -> JSONResponse:
    """Most crowded longs (highest crowdedness_pctile)."""
    await require_auth(request)
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        if not quarter:
            row = conn.execute(
                "SELECT MAX(quarter_end) AS q FROM consensus_snapshots"
            ).fetchone()
            quarter = row["q"] if row and row["q"] else None
        if not quarter:
            return JSONResponse({"quarter_end": None, "rows": []})
        rows = conn.execute(
            """SELECT ticker, issuer_name, n_whales_long, crowdedness_pctile,
                      consensus_score, aggregate_value_usd
                 FROM consensus_snapshots
                WHERE quarter_end=? AND crowdedness_pctile IS NOT NULL
                ORDER BY crowdedness_pctile DESC
                LIMIT ?""",
            (quarter, limit),
        ).fetchall()
    return JSONResponse({"quarter_end": quarter,
                         "rows": [dict(r) for r in rows]})


@app.get("/api/cot")
async def cot(request: Request, market_code: Optional[str] = None,
              limit: int = 26) -> JSONResponse:
    """CFTC Commitment of Traders. Default: latest report across all markets.
    Pass market_code to get a 6-month history for one contract (52 weeks at
    most for sane chart sizes)."""
    await require_auth(request)
    limit = max(1, min(limit, 260))
    with get_conn() as conn:
        if market_code:
            rows = conn.execute(
                """SELECT * FROM cftc_cot
                    WHERE market_code=?
                    ORDER BY report_date DESC LIMIT ?""",
                (market_code.upper(), limit),
            ).fetchall()
        else:
            # Latest report per market.
            rows = conn.execute(
                """SELECT c.* FROM cftc_cot c
                   JOIN (SELECT market_code, MAX(report_date) AS rd
                           FROM cftc_cot GROUP BY market_code) latest
                     ON latest.market_code=c.market_code
                    AND latest.rd=c.report_date
                   ORDER BY c.market_code"""
            ).fetchall()
    return JSONResponse([dict(r) for r in rows])


# ---- watchlist -------------------------------------------------------------

class WatchlistAdd(BaseModel):
    kind: str           # "ticker" or "whale"
    target: str
    note: Optional[str] = None


@app.get("/api/watchlist")
async def watchlist_get(request: Request) -> JSONResponse:
    user = await require_auth(request)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, kind, target, note, created_at FROM user_watchlists "
            "WHERE user_id=? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.post("/api/watchlist")
async def watchlist_add(body: WatchlistAdd, request: Request) -> JSONResponse:
    user = await require_auth(request)
    if body.kind not in ("ticker", "whale"):
        raise HTTPException(400, "kind must be 'ticker' or 'whale'")
    target = body.target.strip()
    if body.kind == "ticker":
        target = target.upper()
    if not target:
        raise HTTPException(400, "target is required")
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO user_watchlists (user_id, kind, target, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user["id"], body.kind, target, body.note, now),
            )
        except Exception:
            raise HTTPException(409, "Already on watchlist")
    return JSONResponse({"ok": True})


@app.delete("/api/watchlist/{wl_id}")
async def watchlist_delete(wl_id: int, request: Request) -> JSONResponse:
    user = await require_auth(request)
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM user_watchlists WHERE id=? AND user_id=?",
            (wl_id, user["id"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Not found")
    return JSONResponse({"ok": True})


# ---- alert rules -----------------------------------------------------------

class AlertRuleBody(BaseModel):
    rule_type: str       # "13d_filed" | "cluster_buy" | "whale_move" | "consensus_cross"
    target: Optional[str] = None
    threshold: Optional[float] = None
    webhook_url: Optional[str] = None
    email: Optional[str] = None


_VALID_RULES = {"13d_filed", "cluster_buy", "whale_move", "consensus_cross"}


@app.get("/api/alerts")
async def alerts_list(request: Request) -> JSONResponse:
    user = await require_auth(request)
    with get_conn() as conn:
        rules = [dict(r) for r in conn.execute(
            "SELECT * FROM alert_rules WHERE user_id=? ORDER BY id DESC",
            (user["id"],),
        ).fetchall()]
        recent = [dict(r) for r in conn.execute(
            """SELECT d.*
                 FROM alert_deliveries d
                 JOIN alert_rules r ON r.id=d.rule_id
                WHERE r.user_id=?
                ORDER BY d.id DESC LIMIT 50""",
            (user["id"],),
        ).fetchall()]
    return JSONResponse({"rules": rules, "recent_deliveries": recent})


@app.post("/api/alerts")
async def alerts_create(body: AlertRuleBody, request: Request) -> JSONResponse:
    user = await require_auth(request)
    if body.rule_type not in _VALID_RULES:
        raise HTTPException(400, f"rule_type must be one of {sorted(_VALID_RULES)}")
    if body.rule_type in {"whale_move"} and not body.target:
        raise HTTPException(400, f"target is required for {body.rule_type}")
    target = body.target.upper() if (body.target and body.rule_type != "whale_move") else body.target
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO alert_rules
                 (user_id, rule_type, target, threshold,
                  webhook_url, email, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (user["id"], body.rule_type, target, body.threshold,
             body.webhook_url, body.email, now),
        )
        rule_id = cur.lastrowid
    return JSONResponse({"ok": True, "id": rule_id})


@app.delete("/api/alerts/{rule_id}")
async def alerts_delete(rule_id: int, request: Request) -> JSONResponse:
    user = await require_auth(request)
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM alert_rules WHERE id=? AND user_id=?",
            (rule_id, user["id"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Not found")
    return JSONResponse({"ok": True})


# ---- websocket -------------------------------------------------------------

@app.websocket("/ws/feed")
async def ws_feed_endpoint(websocket: WebSocket) -> None:
    """Real-time filings stream. The gateway is expected to forward the
    upgrade with x-gateway-secret + x-gateway-user-id headers preserved."""
    await ws_feed.handle(websocket)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
