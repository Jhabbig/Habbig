"""SEC XBRL fundamentals ingest (companyfacts API).

Source: https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json — free,
no key, but requires the polite User-Agent (with contact email) that all our
SEC clients send. Rate-limited like the edgar client: max 5 concurrent,
~120ms min spacing between requests.

We extract a small set of us-gaap / dei concepts (revenue, net income,
diluted EPS, assets, liabilities, equity, operating cash flow, shares
outstanding), accepting the first available tag alias per concept, and store
them under a *canonical* tag name (e.g. "revenue") in `fundamental_fact`.

Caveats worth knowing:
  * companyfacts mixes original and comparative facts (the same period shows
    up again in later filings). We keep the latest-filed value per period.
  * Flow tags (revenue/net income/OCF/EPS) come as durations. Quarterly rows
    are only stored when the duration is ~3 months, annual rows when ~1 year;
    6/9-month YTD durations are skipped so TTM sums stay honest.
  * Q4 usually has no standalone quarterly fact (companies file annual 10-Ks),
    so TTM falls back to the latest annual value when quarterly coverage is
    insufficient. TTM EPS as a sum of 4 quarterly EPS is an approximation
    (ignores share-count drift between quarters).
  * dei EntityCommonStockSharesOutstanding is the cover-page share count,
    which can lag the true current count by a quarter.
  * Dimensional/segment facts are not present in companyfacts; only the
    default (consolidated) members are returned, which is what we want.

The build/test sandbox has no outbound network — every fetch is wrapped so a
failed request logs and returns 0/None instead of raising.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import date, timedelta
from typing import Any

import httpx

import cik_ticker
import db
import events

log = logging.getLogger("xbrl")

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# SEC requires a UA with contact info. Override via env in production.
USER_AGENT = os.environ.get(
    "XBRL_USER_AGENT",
    "narve.ai whale tracker contact@narve.ai",
)

# Refetch a company's facts when its newest stored fact is older than this.
_STALE_DAYS = 90

# 10 req/sec cap on SEC hosts → cap concurrency at 5, ~120ms min spacing.
_sem = asyncio.Semaphore(5)
_last_request_at = 0.0
_request_lock = asyncio.Lock()
_MIN_SPACING_S = 0.12
_TIMEOUT = 30.0

_write_lock = threading.Lock()

# Canonical concept → us-gaap tag aliases, tried in order.
GAAP_ALIASES: dict[str, list[str]] = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
    ],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
}
# Canonical concept → dei tag aliases.
DEI_ALIASES: dict[str, list[str]] = {
    "shares_outstanding": ["EntityCommonStockSharesOutstanding"],
}

# Flow (duration) concepts get TTM treatment; everything else is point-in-time.
FLOW_TAGS = {"revenue", "net_income", "eps_diluted", "ocf"}

_UNIT_PREF: dict[str, tuple[str, ...]] = {
    "eps_diluted": ("USD/shares",),
    "shares_outstanding": ("shares",),
}
_DEFAULT_UNIT_PREF: tuple[str, ...] = ("USD",)

_Q_FPS = {"Q1", "Q2", "Q3", "Q4"}


# ---------------------------------------------------------------- schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fundamental_fact (
    cik        TEXT NOT NULL,
    ticker     TEXT,
    tag        TEXT NOT NULL,      -- canonical concept name ('revenue', ...)
    unit       TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fy         INTEGER,
    fp         TEXT NOT NULL DEFAULT '',
    form       TEXT NOT NULL DEFAULT '',
    value      REAL,
    filed_at   TEXT,
    PRIMARY KEY (cik, tag, unit, period_end, form, fp)
);
CREATE INDEX IF NOT EXISTS idx_fundamental_ticker_tag ON fundamental_fact(ticker, tag, period_end);
CREATE INDEX IF NOT EXISTS idx_fundamental_cik ON fundamental_fact(cik);
"""

_schema_ready = False


def ensure_schema() -> None:
    """Create our tables/indexes if missing. Idempotent and cheap."""
    global _schema_ready
    if _schema_ready:
        return
    with db.connect() as cx:
        cx.executescript(_SCHEMA)
    _schema_ready = True


# ---------------------------------------------------------------- helpers

def _pad_cik(cik: str | int | None) -> str | None:
    """Normalise a CIK to the zero-padded 10-digit form, or None."""
    if cik is None:
        return None
    digits = "".join(ch for ch in str(cik).strip() if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(10)[-10:]


def _days_between(start: str | None, end: str | None) -> int | None:
    try:
        return (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days
    except (TypeError, ValueError):
        return None


def _is_quarterly(fp: str | None, form: str | None) -> bool:
    return (fp or "").upper() in _Q_FPS or (form or "").upper().startswith("10-Q")


def _is_annual(fp: str | None, form: str | None) -> bool:
    return (fp or "").upper() == "FY" or (form or "").upper().startswith("10-K")


def _pick_unit(tag: str, units: dict[str, list]) -> tuple[str, list] | None:
    """Pick the preferred unit series for a concept ('USD', 'USD/shares', ...)."""
    if not units:
        return None
    for pref in _UNIT_PREF.get(tag, _DEFAULT_UNIT_PREF):
        entries = units.get(pref)
        if entries:
            return pref, entries
    for unit, entries in units.items():
        if entries:
            return unit, entries
    return None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov",
        },
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


async def _throttle() -> None:
    global _last_request_at
    async with _request_lock:
        loop = asyncio.get_event_loop()
        now = loop.time()
        wait = _MIN_SPACING_S - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = loop.time()


async def _fetch_companyfacts(cik10: str) -> dict | None:
    url = COMPANYFACTS_URL.format(cik=cik10)
    await _throttle()
    try:
        async with _sem, _client() as cx:
            r = await cx.get(url)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.info("companyfacts fetch failed for CIK %s: %s", cik10, e)
        return None


# ---------------------------------------------------------------- parsing

def _parse_companyfacts(data: dict | None, cik10: str, ticker: str | None) -> list[dict]:
    """Flatten a companyfacts payload into fundamental_fact rows.

    Shape: {facts: {"us-gaap": {Tag: {units: {USD: [{end, val, fy, fp,
    form, filed, start?}, ...]}}}, "dei": {...}}}. For each canonical
    concept we take the first alias that exists, pick the preferred unit,
    and keep quarterly + annual rows (duration-checked for flow tags).
    """
    facts = (data or {}).get("facts") or {}
    out: dict[tuple, dict] = {}

    for namespace, aliases in (("us-gaap", GAAP_ALIASES), ("dei", DEI_ALIASES)):
        ns_facts = facts.get(namespace) or {}
        for canon, tag_names in aliases.items():
            picked = None
            for tag_name in tag_names:
                node = ns_facts.get(tag_name) or {}
                picked = _pick_unit(canon, node.get("units") or {})
                if picked:
                    break
            if not picked:
                continue
            unit, entries = picked
            is_flow = canon in FLOW_TAGS
            for e in entries:
                if not isinstance(e, dict):
                    continue
                end = e.get("end")
                val = e.get("val")
                if end is None or val is None:
                    continue
                fp = (e.get("fp") or "").upper()
                form = e.get("form") or ""
                quarterly = _is_quarterly(fp, form)
                annual = _is_annual(fp, form)
                if not quarterly and not annual:
                    continue
                if is_flow:
                    days = _days_between(e.get("start"), end)
                    if days is not None:
                        if 60 <= days <= 120:
                            if not quarterly:
                                continue  # 3-month comparative inside a 10-K
                        elif 330 <= days <= 400:
                            if not annual:
                                continue
                        else:
                            continue  # 6/9-month YTD or odd stub period
                try:
                    value = float(val)
                except (TypeError, ValueError):
                    continue
                try:
                    fy = int(e.get("fy")) if e.get("fy") is not None else None
                except (TypeError, ValueError):
                    fy = None
                row = {
                    "cik": cik10,
                    "ticker": (ticker or "").upper() or None,
                    "tag": canon,
                    "unit": unit,
                    "period_end": str(end)[:10],
                    "fy": fy,
                    "fp": fp,
                    "form": form,
                    "value": value,
                    "filed_at": e.get("filed") or "",
                }
                key = (row["cik"], row["tag"], row["unit"], row["period_end"],
                       row["form"], row["fp"])
                prev = out.get(key)
                if prev is None or (row["filed_at"] or "") >= (prev["filed_at"] or ""):
                    out[key] = row
    return list(out.values())


def _upsert_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _write_lock, db.connect() as cx:
        cur = cx.executemany(
            """
            INSERT OR REPLACE INTO fundamental_fact (
                cik, ticker, tag, unit, period_end, fy, fp, form, value, filed_at
            ) VALUES (
                :cik, :ticker, :tag, :unit, :period_end, :fy, :fp, :form, :value, :filed_at
            )
            """,
            rows,
        )
        return cur.rowcount


# ---------------------------------------------------------------- ingest

async def fetch_company(cik: str, ticker: str | None = None) -> int:
    """Fetch companyfacts for one issuer and upsert. Returns rows upserted.

    Resolves the ticker via cik_ticker when not given. Never raises on
    network failure — logs and returns 0.
    """
    ensure_schema()
    cik10 = _pad_cik(cik)
    if not cik10:
        log.info("fetch_company: unusable cik %r", cik)
        return 0

    if not ticker:
        try:
            await cik_ticker.ensure_loaded()
        except Exception as e:  # ensure_loaded guards internally; belt+braces
            log.info("cik_ticker load failed: %s", e)
        ticker = cik_ticker.lookup_ticker(cik10)

    data = await _fetch_companyfacts(cik10)
    if not data:
        return 0

    try:
        rows = _parse_companyfacts(data, cik10, ticker)
    except Exception as e:
        log.warning("companyfacts parse failed for CIK %s: %s", cik10, e)
        return 0

    n = _upsert_rows(rows)
    if n:
        log.info("xbrl: CIK %s (%s) → %d fact rows", cik10, ticker or "?", n)
        events.broadcast("xbrl_facts", {"cik": cik10, "ticker": ticker, "rows": n})
    return n


async def refresh_for_tracked(limit: int = 10) -> int:
    """Fetch facts for issuers seen in insider_txn that are missing or stale.

    Picks up to `limit` distinct issuer CIKs that either have no
    fundamental_fact rows yet or whose newest filed_at is older than
    ~90 days, most-recently-active issuers first. Returns total rows.
    """
    ensure_schema()
    with db.connect() as cx:
        issuers = cx.execute(
            """
            SELECT issuer_cik AS cik,
                   MAX(issuer_ticker) AS ticker,
                   MAX(filed_at)      AS newest_txn
            FROM insider_txn
            WHERE issuer_cik IS NOT NULL AND issuer_cik != ''
            GROUP BY issuer_cik
            ORDER BY newest_txn DESC
            """
        ).fetchall()
        have = cx.execute(
            "SELECT cik, MAX(filed_at) AS newest FROM fundamental_fact GROUP BY cik"
        ).fetchall()

    newest_by_cik = {r["cik"]: (r["newest"] or "") for r in have}
    cutoff = (date.today() - timedelta(days=_STALE_DAYS)).isoformat()

    todo: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for r in issuers:
        cik10 = _pad_cik(r["cik"])
        if not cik10 or cik10 in seen:
            continue
        seen.add(cik10)
        newest = newest_by_cik.get(cik10)
        if newest is None or newest[:10] < cutoff:
            todo.append((cik10, r["ticker"]))
        if len(todo) >= max(0, int(limit)):
            break

    total = 0
    for cik10, ticker in todo:
        total += await fetch_company(cik10, ticker)
    return total


# ---------------------------------------------------------------- snapshots

def _dedupe_periods(rows: list[dict]) -> list[dict]:
    """One row per period_end (latest filed wins), newest period first."""
    best: dict[str, dict] = {}
    for r in rows:
        pe = r["period_end"]
        cur = best.get(pe)
        if cur is None or (r.get("filed_at") or "") > (cur.get("filed_at") or ""):
            best[pe] = r
    return sorted(best.values(), key=lambda r: r["period_end"], reverse=True)


def _flow_ttm(rows: list[dict], tag: str) -> tuple[float | None, float | None]:
    """(ttm, prior_year_ttm) for a flow tag.

    TTM = sum of the 4 most recent quarterly values; prior-year TTM = sum of
    quarters 5-8. With fewer than 4 quarters we fall back to the latest
    annual value (and the previous annual as the prior-year figure).
    """
    tagged = [r for r in rows
              if r.get("tag") == tag and r.get("value") is not None and r.get("period_end")]
    q = _dedupe_periods([r for r in tagged if _is_quarterly(r.get("fp"), r.get("form"))])
    if len(q) >= 4:
        ttm = float(sum(r["value"] for r in q[:4]))
        prior = float(sum(r["value"] for r in q[4:8])) if len(q) >= 8 else None
        return ttm, prior
    a = _dedupe_periods([r for r in tagged if _is_annual(r.get("fp"), r.get("form"))])
    if a:
        prior = float(a[1]["value"]) if len(a) >= 2 else None
        return float(a[0]["value"]), prior
    return None, None


def _latest_point(rows: list[dict], tag: str) -> float | None:
    tagged = [r for r in rows
              if r.get("tag") == tag and r.get("value") is not None and r.get("period_end")]
    d = _dedupe_periods(tagged)
    return float(d[0]["value"]) if d else None


def compute_snapshot_from_rows(rows: list[dict], price: float | None) -> dict | None:
    """Pure snapshot math over fundamental_fact-shaped rows. Unit-testable.

    Each row needs: tag, period_end, fp, form, value, filed_at (and
    optionally cik/ticker, surfaced into the result when present).
    Returns the shared fundamentals-snapshot contract dict, or None when
    rows is empty.
    """
    if not rows:
        return None

    ticker = next((r.get("ticker") for r in rows if r.get("ticker")), None)
    cik = next((r.get("cik") for r in rows if r.get("cik")), None)
    as_of = max((r.get("period_end") or "" for r in rows), default="") or None

    revenue_ttm, prior_revenue_ttm = _flow_ttm(rows, "revenue")
    net_income_ttm, _ = _flow_ttm(rows, "net_income")
    eps_ttm, _ = _flow_ttm(rows, "eps_diluted")
    ocf_ttm, _ = _flow_ttm(rows, "ocf")

    assets = _latest_point(rows, "assets")
    liabilities = _latest_point(rows, "liabilities")
    equity = _latest_point(rows, "equity")
    shares = _latest_point(rows, "shares_outstanding")

    net_margin = None
    if net_income_ttm is not None and revenue_ttm is not None and revenue_ttm != 0:
        net_margin = net_income_ttm / revenue_ttm

    roe = None
    if net_income_ttm is not None and equity is not None and equity != 0:
        roe = net_income_ttm / equity

    revenue_growth_yoy = None
    if revenue_ttm is not None and prior_revenue_ttm is not None and prior_revenue_ttm > 0:
        revenue_growth_yoy = revenue_ttm / prior_revenue_ttm - 1.0

    market_cap = None
    if price is not None and shares is not None:
        market_cap = price * shares

    pe = None
    if market_cap is not None and net_income_ttm is not None and net_income_ttm > 0:
        pe = market_cap / net_income_ttm

    pb = None
    if market_cap is not None and equity is not None and equity > 0:
        pb = market_cap / equity

    return {
        "ticker": ticker,
        "cik": cik,
        "as_of": as_of,
        "revenue_ttm": revenue_ttm,
        "net_income_ttm": net_income_ttm,
        "eps_ttm": eps_ttm,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "shares_outstanding": shares,
        "operating_cash_flow_ttm": ocf_ttm,
        "net_margin": net_margin,
        "roe": roe,
        "revenue_growth_yoy": revenue_growth_yoy,
        "price": price,
        "market_cap": market_cap,
        "pe": pe,
        "pb": pb,
    }


def snapshot(ticker: str) -> dict | None:
    """Fundamentals snapshot for one ticker per the shared contract.

    None when we hold no facts for the ticker. Price is the latest close
    from the local price_daily cache (None if we have no prices).
    """
    ensure_schema()
    t = (ticker or "").strip().upper()
    if not t:
        return None
    with db.connect() as cx:
        rows = cx.execute(
            """
            SELECT cik, ticker, tag, unit, period_end, fy, fp, form, value, filed_at
            FROM fundamental_fact
            WHERE ticker = ?
            """,
            (t,),
        ).fetchall()
        prow = cx.execute(
            "SELECT close FROM price_daily WHERE ticker = ? ORDER BY date DESC LIMIT 1",
            (t,),
        ).fetchone()
    if not rows:
        return None
    price = float(prow["close"]) if prow and prow["close"] is not None else None
    snap = compute_snapshot_from_rows([dict(r) for r in rows], price)
    if snap is not None:
        snap["ticker"] = t
    return snap


def snapshots_bulk(tickers: list[str]) -> dict[str, dict]:
    """Snapshot many tickers; tickers without data are silently skipped."""
    ensure_schema()
    out: dict[str, dict] = {}
    for t in tickers or []:
        key = (t or "").strip().upper()
        if not key or key in out:
            continue
        snap = snapshot(key)
        if snap is not None:
            out[key] = snap
    return out
