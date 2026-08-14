"""Cross-signal screening engine over the tracked-ticker universe.

Data source: purely the local SQLite database, read through two sibling
modules — `signals.ticker_synthesis()` for filing-derived activity signals
(insider buys/sells, activist stakes, M&A 8-Ks, 13F holders, congress
trades, options flow, dark pool prints) and `xbrl.snapshot()` for the
fundamentals snapshot (TTM revenue / net income, balance sheet, valuation
ratios per the shared FUNDAMENTALS SNAPSHOT CONTRACT). This module does
NO network I/O of its own; everything it touches was previously ingested
and cached by the fetcher modules.

Caveats:
  - `xbrl.snapshot()` is noticeably heavier than the signal queries, so
    fundamentals are computed lazily: only for tickers that survive the
    cheap signal-level filters, and only when a condition or the sort key
    actually references a fundamentals field.
  - Fundamentals coverage is spotty — not every tracked ticker has XBRL
    facts or cached prices. A row whose referenced field is None is
    excluded whenever any condition references that field, so screens on
    e.g. `pe` silently shrink the universe to tickers with usable data.
  - The universe is every distinct ticker seen in any tracked feed,
    capped at SCREENER_UNIVERSE_CAP (env, default 300) alphabetically for
    determinism. It is not "the market" — only names whales touched.
  - `xbrl` may be absent in partial deployments; importing this module
    still works, and only fundamentals-referencing screens fail (with a
    clear RuntimeError the server maps to an error response).
"""

from __future__ import annotations

import csv
import io
import logging
import operator
import os
import sqlite3
from typing import Any, Callable

import db
import signals

try:
    import xbrl
except ImportError:  # pragma: no cover - partial deployment / build order
    xbrl = None  # type: ignore[assignment]

log = logging.getLogger("screener")

UNIVERSE_CAP = int(os.environ.get("SCREENER_UNIVERSE_CAP", "300"))

# ---------------------------------------------------------------------------
# Legal fields
# ---------------------------------------------------------------------------

# Fundamentals fields — exactly the FUNDAMENTALS SNAPSHOT CONTRACT keys.
_FUNDAMENTAL_DESCRIPTIONS: dict[str, str] = {
    "ticker":                  "Ticker symbol",
    "cik":                     "SEC CIK of the issuer",
    "as_of":                   "ISO date of the latest reported period end",
    "revenue_ttm":             "Trailing-twelve-month revenue (USD)",
    "net_income_ttm":          "Trailing-twelve-month net income (USD)",
    "eps_ttm":                 "Trailing-twelve-month earnings per share",
    "assets":                  "Total assets (USD)",
    "liabilities":             "Total liabilities (USD)",
    "equity":                  "Total stockholders' equity (USD)",
    "shares_outstanding":      "Shares outstanding",
    "operating_cash_flow_ttm": "Trailing-twelve-month operating cash flow (USD)",
    "net_margin":              "Net income TTM / revenue TTM",
    "roe":                     "Return on equity (net income TTM / equity)",
    "revenue_growth_yoy":      "Revenue TTM vs prior-year TTM, as a fraction",
    "price":                   "Latest cached daily close (USD)",
    "market_cap":              "Price x shares outstanding (USD)",
    "pe":                      "Market cap / net income TTM (None if NI <= 0)",
    "pb":                      "Market cap / equity (None if equity <= 0)",
}

# Signal fields — the scalar keys of signals.ticker_synthesis().
_SIGNAL_DESCRIPTIONS: dict[str, str] = {
    "synthesis_score":     "Composite whale-activity score across all feeds",
    "insider_buy_count":   "Insider open-market buys in the window",
    "insider_sell_count":  "Insider open-market sells in the window",
    "activist_count":      "Activist 13D/G filings in the window",
    "ma_event_count":      "M&A-flavored 8-K events in the window",
    "fund_holder_count":   "Distinct 13F funds holding the ticker",
    "congress_buy_count":  "Congressional purchases in the window",
    "congress_sell_count": "Congressional sales in the window",
    "options_call_count":  "Unusual call-side options alerts in the window",
    "options_put_count":   "Unusual put-side options alerts in the window",
    "options_premium":     "Aggregate options premium in the window (USD)",
    "dark_pool_premium":   "Aggregate dark pool print premium in the window (USD)",
}

#: Every legal screen/sort field mapped to a short human description.
FIELDS: dict[str, str] = {**_FUNDAMENTAL_DESCRIPTIONS, **_SIGNAL_DESCRIPTIONS}

_SIGNAL_FIELDS: frozenset[str] = frozenset(_SIGNAL_DESCRIPTIONS)
# `ticker` is known locally, so it never forces a snapshot fetch.
_FUNDAMENTAL_FIELDS: frozenset[str] = frozenset(_FUNDAMENTAL_DESCRIPTIONS) - {"ticker"}

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "gt":  operator.gt,
    "gte": operator.ge,
    "lt":  operator.lt,
    "lte": operator.le,
    "eq":  operator.eq,
    "neq": operator.ne,
}

# (table, ticker column) pairs that define the tracked-ticker universe.
_UNIVERSE_SOURCES: tuple[tuple[str, str], ...] = (
    ("insider_txn",    "issuer_ticker"),
    ("activist_stake", "issuer_ticker"),
    ("ma_event",       "issuer_ticker"),
    ("fund_holding",   "issuer_ticker"),
    ("congress_trade", "ticker"),
)

_schema_ready = False


def ensure_schema() -> None:
    """No-op — the screener owns no tables; kept for interface parity.

    Every other module exposes ensure_schema(); the server calls it
    unconditionally, so this must exist, be idempotent, and be free.
    """
    global _schema_ready
    if _schema_ready:
        return
    _schema_ready = True


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def universe(cap: int | None = None) -> list[str]:
    """Distinct tickers seen in any tracked feed, alphabetical, capped."""
    ensure_schema()
    cap = UNIVERSE_CAP if cap is None else max(1, int(cap))
    tickers: set[str] = set()
    with db.connect() as cx:
        for table, col in _UNIVERSE_SOURCES:
            try:
                rows = cx.execute(
                    f"SELECT DISTINCT {col} AS t FROM {table} "
                    f"WHERE {col} IS NOT NULL AND {col} <> ''"
                ).fetchall()
            except sqlite3.Error as exc:
                # A feed's table may not exist yet (its module never ran).
                log.debug("universe: skipping %s.%s: %s", table, col, exc)
                continue
            tickers.update(r["t"] for r in rows if r["t"])
    return sorted(tickers)[:cap]


# ---------------------------------------------------------------------------
# Condition validation / evaluation
# ---------------------------------------------------------------------------

def _validate_conditions(conditions: list[dict] | None) -> list[dict[str, Any]]:
    """Normalise and validate a condition spec; ValueError on bad input."""
    if conditions is None:
        return []
    if not isinstance(conditions, list):
        raise ValueError("conditions must be a list of objects")
    out: list[dict[str, Any]] = []
    for i, cond in enumerate(conditions):
        if not isinstance(cond, dict):
            raise ValueError(f"condition #{i} must be an object")
        field = cond.get("field")
        op = cond.get("op")
        value = cond.get("value")
        if field not in FIELDS:
            raise ValueError(f"unknown field: {field!r}")
        if op not in _OPS:
            raise ValueError(
                f"unknown op: {op!r} (expected one of {', '.join(sorted(_OPS))})"
            )
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"condition #{i}: value must be a number")
        out.append({"field": field, "op": op, "value": value})
    return out


def _passes(row: dict[str, Any], cond: dict[str, Any]) -> bool:
    """True if the row satisfies the condition. None field => excluded."""
    val = row.get(cond["field"])
    if val is None:
        return False
    try:
        return bool(_OPS[cond["op"]](val, cond["value"]))
    except TypeError:
        # Incomparable types (e.g. numeric op against a string field):
        # treat like a missing value and exclude the row.
        return False


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------

def _signal_row(ticker: str, window_days: int) -> dict[str, Any]:
    """Cheap, local signal fields for one ticker (never raises)."""
    try:
        synth = signals.ticker_synthesis(ticker, window_days=window_days)
    except Exception as exc:
        log.warning("ticker_synthesis failed for %s: %s", ticker, exc)
        synth = {}
    row: dict[str, Any] = {"ticker": ticker}
    for key in _SIGNAL_FIELDS:
        row[key] = synth.get(key)
    return row


def _snapshot(ticker: str) -> dict[str, Any] | None:
    """Fundamentals snapshot for one ticker (never raises)."""
    if xbrl is None:
        return None
    try:
        return xbrl.snapshot(ticker)
    except Exception as exc:
        log.warning("xbrl.snapshot failed for %s: %s", ticker, exc)
        return None


def run_screen(
    conditions: list[dict],
    sort: str = "synthesis_score",
    desc: bool = True,
    limit: int = 50,
    window_days: int = 90,
) -> list[dict[str, Any]]:
    """Filter the tracked-ticker universe by AND-ed conditions.

    Each result row is the merge of the ticker's signal fields and (when
    fundamentals were needed) its snapshot fields. Fundamentals are only
    computed for tickers that already passed the cheap signal-level
    filters, and only if a condition or the sort key references a
    fundamentals field.
    """
    ensure_schema()
    conds = _validate_conditions(conditions)
    if sort not in FIELDS:
        raise ValueError(f"unknown sort field: {sort!r}")
    limit = max(1, min(int(limit), 1000))
    window_days = max(1, int(window_days))

    signal_conds = [c for c in conds if c["field"] not in _FUNDAMENTAL_FIELDS]
    fund_conds = [c for c in conds if c["field"] in _FUNDAMENTAL_FIELDS]
    needs_fundamentals = bool(fund_conds) or sort in _FUNDAMENTAL_FIELDS

    if needs_fundamentals and xbrl is None:
        raise RuntimeError(
            "fundamentals fields were requested but the xbrl module is not "
            "available in this deployment"
        )

    # Pass 1 — cheap signal fields over the whole universe.
    rows: list[dict[str, Any]] = []
    for ticker in universe():
        row = _signal_row(ticker, window_days)
        if all(_passes(row, c) for c in signal_conds):
            rows.append(row)

    # Pass 2 — fundamentals, only for survivors and only when referenced.
    if needs_fundamentals:
        enriched: list[dict[str, Any]] = []
        for row in rows:
            snap = _snapshot(row["ticker"])
            if snap:
                merged = {**snap, **row}  # signal fields + ticker win on clash
            else:
                merged = row  # missing keys read as None via .get()
            if all(_passes(merged, c) for c in fund_conds):
                enriched.append(merged)
        rows = enriched

    # Sort with None values last, regardless of direction.
    present = [r for r in rows if r.get(sort) is not None]
    absent = [r for r in rows if r.get(sort) is None]
    try:
        present.sort(key=lambda r: r[sort], reverse=bool(desc))
    except TypeError:
        present.sort(key=lambda r: str(r[sort]), reverse=bool(desc))
    return (present + absent)[:limit]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def to_csv(rows: list[dict]) -> str:
    """Serialise screen results to CSV: ticker first, then sorted keys."""
    ensure_schema()
    keys: set[str] = set()
    for row in rows:
        keys.update(row.keys())
    keys.discard("ticker")
    header = ["ticker"] + sorted(keys)
    sio = io.StringIO()
    writer = csv.DictWriter(sio, fieldnames=header, restval="", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return sio.getvalue()
