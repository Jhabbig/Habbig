"""Market-data provider abstraction — live-ish quotes for the dashboard UI.

Two providers behind one interface:

  stooq   (default) — free, no key, delayed EOD/last-price via the CSV quote
          endpoint  https://stooq.com/q/l/?s=<sym>&f=sd2t2ohlcv&h&e=csv
          where <sym> follows the same lowercase `.us` convention as
          prices.stooq_symbol (AAPL → aapl.us). Quotes are delayed and the
          endpoint has no previous-close column, so `close_prev` is filled
          from our local `price_daily` cache when available.

  polygon (activates automatically when POLYGON_API_KEY is set) — REST
          snapshot  GET /v2/snapshot/locale/us/markets/stocks/tickers/{T}
          plus an optional WebSocket trade stream (wss://socket.polygon.io)
          for the tickers listed in MARKET_DATA_WS_TICKERS.

Caveats:
  - Every network call is wrapped in try/except that logs and returns
    None/empty — quote fetches must never crash the dashboard (the build
    sandbox has no outbound network at all).
  - Quotes are cached in memory with a short TTL (QUOTE_CACHE_TTL_S,
    default 5 s realtime / 60 s stooq) so repeated UI polls are cheap.
    On fetch failure we serve the stale cached quote rather than nothing.
  - Polygon snapshot fields are read defensively — free-tier keys return
    partial snapshots (e.g. no lastTrade), and missing fields become None.
  - No DB tables are owned by this module; ensure_schema() is a no-op that
    exists for interface consistency with the sibling modules.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import time
from typing import Any

import httpx

import db
import events
import prices

log = logging.getLogger("market_data")

# ─── Config ───────────────────────────────────────────────────────────

POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "").strip()

USER_AGENT = "narve.ai whale tracker contact@narve.ai"
_STOOQ_QUOTE_URL = "https://stooq.com/q/l/"
_POLYGON_BASE = "https://api.polygon.io"
_POLYGON_WS_URL = "wss://socket.polygon.io/stocks"
_TIMEOUT = 15.0

_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0

_sem = asyncio.Semaphore(5)

# ticker (upper) → (monotonic timestamp, quote dict)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_schema_ready = False


def ensure_schema() -> None:
    """No-op — quotes live purely in memory. Kept for interface consistency."""
    global _schema_ready
    if _schema_ready:
        return
    _schema_ready = True


# ─── Public API ───────────────────────────────────────────────────────

def provider_name() -> str:
    """Active quote provider: 'polygon' when POLYGON_API_KEY is set, else 'stooq'."""
    return "polygon" if POLYGON_API_KEY else "stooq"


def is_realtime_configured() -> bool:
    return bool(POLYGON_API_KEY)


async def get_quote(ticker: str) -> dict[str, Any] | None:
    """Return a quote dict for `ticker`, or None when nothing is available.

    Keys: ticker, price, open, high, low, close_prev, volume, as_of (ISO
    string), provider, realtime (bool). Missing fields are None.
    """
    ensure_schema()
    t = (ticker or "").strip().upper()
    if not t:
        return None

    ttl = _cache_ttl()
    cached = _cache.get(t)
    now = time.monotonic()
    if cached and (now - cached[0]) < ttl:
        return dict(cached[1])

    if POLYGON_API_KEY:
        quote = await _fetch_polygon(t)
    else:
        quote = await _fetch_stooq(t)

    if quote is None:
        # Fetch failed / no data — a stale quote beats no quote.
        return dict(cached[1]) if cached else None

    _cache[t] = (time.monotonic(), quote)
    return dict(quote)


async def get_quotes(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch quotes for many tickers concurrently (network capped at 5).

    Returns {TICKER: quote}; tickers with no available quote are omitted.
    """
    ensure_schema()
    unique = sorted({(t or "").strip().upper() for t in tickers if t and t.strip()})
    out: dict[str, dict[str, Any]] = {}

    async def one(t: str) -> None:
        q = await get_quote(t)
        if q is not None:
            out[t] = q

    await asyncio.gather(*(one(t) for t in unique))
    return out


async def run_ws_forever() -> None:
    """Background task — Polygon WebSocket trade stream.

    Subscribes to trades for the tickers in MARKET_DATA_WS_TICKERS
    (comma-separated; empty = do not connect), updates the in-memory
    quote cache on every trade and broadcasts a `quote` event so SSE
    clients see live prices. Reconnects with exponential backoff 1..60 s.
    """
    ensure_schema()
    try:
        import websockets  # imported lazily; not a hard dep
    except ImportError:
        log.warning("websockets not installed — realtime quote stream disabled. "
                    "pip install websockets to enable.")
        return

    if not POLYGON_API_KEY:
        log.info("POLYGON_API_KEY not set — quote WS subscriber not started")
        return
    tickers = _ws_tickers()
    if not tickers:
        log.info("MARKET_DATA_WS_TICKERS empty — quote WS subscriber not started")
        return

    sub_params = ",".join(f"T.{t}" for t in tickers)
    backoff = _BACKOFF_INITIAL
    log.info("polygon quote WS starting: url=%s tickers=%s", _POLYGON_WS_URL, tickers)

    while True:
        try:
            async with websockets.connect(
                _POLYGON_WS_URL,
                ping_interval=30,
                ping_timeout=20,
                close_timeout=5,
                max_size=2 * 1024 * 1024,
            ) as ws:
                await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                await ws.send(json.dumps({"action": "subscribe", "params": sub_params}))
                backoff = _BACKOFF_INITIAL

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    batch = msg if isinstance(msg, list) else [msg]
                    for ev in batch:
                        if not isinstance(ev, dict):
                            continue
                        et = ev.get("ev")
                        if et == "T":
                            _apply_ws_trade(ev)
                        elif et == "status":
                            status = str(ev.get("status") or "")
                            if status == "auth_failed":
                                log.warning("polygon ws auth failed: %s — giving up",
                                            ev.get("message"))
                                return
                            log.info("polygon ws status: %s %s",
                                     status, ev.get("message") or "")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("polygon ws disconnected: %s; reconnecting in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)


# ─── Providers ────────────────────────────────────────────────────────

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


async def _fetch_stooq(ticker: str) -> dict[str, Any] | None:
    """Delayed last-price quote from the Stooq CSV quote endpoint."""
    sym = prices.stooq_symbol(ticker)
    if not sym:
        return None
    url = f"{_STOOQ_QUOTE_URL}?s={sym}&f=sd2t2ohlcv&h&e=csv"
    try:
        async with _sem, _client() as cx:
            r = await cx.get(url)
            r.raise_for_status()
            body = r.text
    except Exception as e:
        log.info("stooq quote %s failed: %s", ticker, e)
        return None

    rec = _parse_stooq_csv(body)
    if not rec:
        return None
    price = _f(rec.get("close"))
    if price is None:  # unknown ticker → row of N/D
        return None

    date = rec.get("date") or ""
    tm = rec.get("time") or ""
    if len(date) == 10 and date[4] == "-":
        as_of = f"{date}T{tm}" if len(tm) == 8 and tm[2] == ":" else date
    else:
        as_of = _now_iso()

    vol = _f(rec.get("volume"))
    return {
        "ticker": ticker,
        "price": price,
        "open": _f(rec.get("open")),
        "high": _f(rec.get("high")),
        "low": _f(rec.get("low")),
        "close_prev": _prev_close_local(ticker, before_date=date or None),
        "volume": int(vol) if vol is not None else None,
        "as_of": as_of,
        "provider": "stooq",
        "realtime": False,
    }


def _parse_stooq_csv(body: str) -> dict[str, str] | None:
    """Parse the 2-line Stooq quote CSV (Symbol,Date,Time,Open,High,Low,Close,Volume)."""
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in lines[0].split(",")]
    values = [v.strip() for v in lines[1].split(",")]
    if "close" not in header:
        return None
    return dict(zip(header, values))


async def _fetch_polygon(ticker: str) -> dict[str, Any] | None:
    """Snapshot quote from Polygon's REST API. All fields read defensively."""
    url = f"{_POLYGON_BASE}/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
    try:
        async with _sem, _client() as cx:
            r = await cx.get(url, params={"apiKey": POLYGON_API_KEY})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.info("polygon snapshot %s failed: %s", ticker, e)
        return None

    tk = data.get("ticker") if isinstance(data, dict) else None
    if not isinstance(tk, dict):
        return None
    day = tk.get("day") if isinstance(tk.get("day"), dict) else {}
    last = tk.get("lastTrade") if isinstance(tk.get("lastTrade"), dict) else {}
    prev = tk.get("prevDay") if isinstance(tk.get("prevDay"), dict) else {}

    price = _f(last.get("p"))
    if price is None:
        price = _f(day.get("c"))
    if price is None:
        price = _f(prev.get("c"))
    if price is None:
        return None

    vol = _f(day.get("v"))
    return {
        "ticker": ticker,
        "price": price,
        "open": _f(day.get("o")),
        "high": _f(day.get("h")),
        "low": _f(day.get("l")),
        "close_prev": _f(prev.get("c")),
        "volume": int(vol) if vol is not None else None,
        "as_of": _ts_to_iso(last.get("t")) or _ts_to_iso(tk.get("updated")) or _now_iso(),
        "provider": "polygon",
        "realtime": True,
    }


# ─── WS cache updates ─────────────────────────────────────────────────

def _apply_ws_trade(ev: dict[str, Any]) -> None:
    """Fold a Polygon trade event into the quote cache and broadcast it."""
    sym = str(ev.get("sym") or "").strip().upper()
    price = _f(ev.get("p"))
    if not sym or price is None:
        return
    cached = _cache.get(sym)
    quote: dict[str, Any] = dict(cached[1]) if cached else _empty_quote(sym)
    quote["price"] = price
    quote["as_of"] = _ts_to_iso(ev.get("t")) or _now_iso()
    quote["provider"] = "polygon"
    quote["realtime"] = True
    _cache[sym] = (time.monotonic(), quote)
    events.broadcast("quote", quote)


def _empty_quote(ticker: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "price": None,
        "open": None,
        "high": None,
        "low": None,
        "close_prev": None,
        "volume": None,
        "as_of": None,
        "provider": provider_name(),
        "realtime": is_realtime_configured(),
    }


# ─── Helpers ──────────────────────────────────────────────────────────

def _cache_ttl() -> float:
    raw = os.environ.get("QUOTE_CACHE_TTL_S", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            log.warning("bad QUOTE_CACHE_TTL_S=%r — using default", raw)
    return 5.0 if is_realtime_configured() else 60.0


def _ws_tickers() -> list[str]:
    raw = os.environ.get("MARKET_DATA_WS_TICKERS", "").strip()
    return sorted({t.strip().upper() for t in raw.split(",") if t.strip()})


def _f(v: Any) -> float | None:
    """Defensive float parse — Stooq uses 'N/D' for missing values."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts_to_iso(v: Any) -> str | None:
    """Epoch timestamp (s / ms / µs / ns — normalised by magnitude) → ISO UTC."""
    ts = _f(v)
    if ts is None or ts <= 0:
        return None
    while ts > 1e11:  # anything past ~year 5138 in seconds must be a finer unit
        ts /= 1000.0
    try:
        return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _prev_close_local(ticker: str, *, before_date: str | None) -> float | None:
    """Previous close from the local price_daily cache (prices.py fills it).

    Stooq's quote endpoint has no previous-close column, so we look it up
    locally; returns None when we hold no history for the ticker.
    """
    try:
        with db.connect() as cx:
            if before_date:
                row = cx.execute(
                    "SELECT close FROM price_daily WHERE ticker = ? AND date < ? "
                    "ORDER BY date DESC LIMIT 1",
                    (ticker.upper(), before_date),
                ).fetchone()
            else:
                row = cx.execute(
                    "SELECT close FROM price_daily WHERE ticker = ? "
                    "ORDER BY date DESC LIMIT 1",
                    (ticker.upper(),),
                ).fetchone()
        if row is None or row["close"] is None:
            return None
        return float(row["close"])
    except Exception as e:
        log.debug("local prev-close lookup %s failed: %s", ticker, e)
        return None
