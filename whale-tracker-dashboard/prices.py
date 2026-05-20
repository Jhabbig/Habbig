"""Daily-price fetcher for Bayesian skill computation.

Source: Stooq (https://stooq.com). Free, no key, CSV over HTTP. US tickers
use the `.us` suffix convention — AAPL → aapl.us, NVDA → nvda.us. Some
share classes need normalisation (BRK.B → brk-b.us). For our needs the
simple lowercase + .us is enough for the vast majority of tickers we see.

Stooq is permissive but not unlimited; we add a 5-concurrent cap to avoid
hammering them. Each ticker is fetched once into the local SQLite cache
(table `price_daily`) and then queried locally.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

import db

log = logging.getLogger("prices")

# SPY proxies the broad-market benchmark; we always co-fetch it so we can
# compute relative alpha.
BENCHMARK_TICKER = "SPY"

USER_AGENT = os.environ.get(
    "PRICES_USER_AGENT",
    "narve.ai whale tracker contact@narve.ai",
)
_BASE_URL = "https://stooq.com/q/d/l/"
_TIMEOUT = 20.0
_sem = asyncio.Semaphore(5)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


_TICKER_BAD = re.compile(r"[^a-z0-9\-]")


def stooq_symbol(ticker: str) -> str | None:
    """AAPL → 'aapl.us'. Returns None on unsupported tickers."""
    if not ticker:
        return None
    t = ticker.lower().replace(".", "-").strip()
    t = _TICKER_BAD.sub("", t)
    if not t:
        return None
    return f"{t}.us"


async def fetch_ticker(ticker: str, lookback_days: int = 400) -> int:
    """Fetch daily closes for `ticker` and persist. Returns rows inserted.

    Stooq's `d` interval returns daily OHLCV. We pull lookback_days back
    and store only Date + Close.
    """
    sym = stooq_symbol(ticker)
    if not sym:
        return 0
    url = f"{_BASE_URL}?s={sym}&i=d"
    try:
        async with _sem, _client() as cx:
            r = await cx.get(url)
            r.raise_for_status()
            body = r.text
    except Exception as e:
        log.info("stooq fetch %s failed: %s", ticker, e)
        return 0

    rows = _parse_csv(body, lookback_days=lookback_days)
    if not rows:
        return 0
    return db.upsert_prices(ticker.upper(), rows)


def _parse_csv(body: str, *, lookback_days: int) -> list[tuple[str, float]]:
    """Parse a Stooq CSV body. Returns the last `lookback_days` (date, close)."""
    if not body or "Date" not in body:
        return []
    out: list[tuple[str, float]] = []
    for line in body.splitlines()[1:]:  # skip header
        parts = line.split(",")
        if len(parts) < 5:
            continue
        date = parts[0].strip()
        try:
            close = float(parts[4])
        except (ValueError, IndexError):
            continue
        # Stooq dates are YYYY-MM-DD already.
        if len(date) != 10 or date[4] != "-":
            continue
        out.append((date, close))
    # Stooq CSVs are date-ascending; take the tail.
    return out[-lookback_days:]


async def ensure_prices_for(tickers: list[str]) -> dict[str, int]:
    """Fetch any tickers we don't already have local prices for.

    Heuristic: if we have at least one row for the ticker in the last
    400 days, assume we already have its history. Otherwise pull.
    """
    out: dict[str, int] = {}
    unique = sorted({(t or "").upper() for t in tickers if t})
    # Always ensure the benchmark exists too.
    if BENCHMARK_TICKER not in unique:
        unique.append(BENCHMARK_TICKER)

    async def one(t: str):
        if _have_recent(t):
            return
        n = await fetch_ticker(t)
        if n:
            out[t] = n

    await asyncio.gather(*(one(t) for t in unique))
    return out


def _have_recent(ticker: str) -> bool:
    with db.connect() as cx:
        row = cx.execute(
            "SELECT 1 FROM price_daily WHERE ticker = ? "
            "AND date >= date('now', '-30 days') LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
    return row is not None
