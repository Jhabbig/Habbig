"""Options-implied equity forecasts (venue='options').

Turns listed option chains into probabilistic events: for each watchlist
symbol and near expiry, emits markets like "AAPL closes >= $230 on
2026-09-18" whose baseline probability is the RISK-NEUTRAL P(S_T > K)
derived from that strike's implied volatility (N(d2)). These are the
equity market's own probabilities — displayed and calibration-scored like
any other venue, never trade advice.

Contract (same as the other ingest modules): async fetch_horizon(horizon_days)
returns a list of normalized market dicts:
  {venue: 'options', venue_id, event_ticker, question, category: 'finance',
   url, end_date (ISO Z), yes_bid: None, yes_ask: None, last_price: <prob>,
   liquidity, volume_24h, yes_token_id: None, no_token_id: None}
last_price carries the risk-neutral probability (there is no book), so
compute_baseline falls through to it and tiers the row 'stale'; the module
sets liquidity from open interest so rows sort sensibly.

Implementation notes:
- Chains come from Yahoo's public v7 options endpoint (query2 with query1
  fallback, browser User-Agent per the vendored pattern in models_fed.py).
  Some Yahoo edges demand a cookie+crumb pair; on 401/403 we do the standard
  dance once per cycle (fc.yahoo.com sets the cookie, /v1/test/getcrumb
  returns the crumb) and retry with ?crumb=.
- r = 0.04 constant: a short-rate proxy — at day-to-week horizons the rate
  term moves d2 by well under a probability point, so precision is wasted.
- end_date = expiry date 21:00:00Z approximates the 16:00 ET close (exact
  under DST, off by one hour in winter — immaterial at these horizons).
- All model inputs (spot S, strike K, contract IV, T, r) are recoverable:
  K and expiry live in the venue_id, S/IV from the venue at expiry.
- Output is bounded: <=2 expiries and <=10 strikes per symbol, and a global
  cap of 60 rows applied round-robin across symbols so a long watchlist
  degrades evenly instead of dropping its tail.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import date, datetime, timezone

import httpx

log = logging.getLogger(__name__)

DEFAULT_WATCHLIST = "SPY,QQQ,AAPL,MSFT,NVDA,TSLA,AMZN,GOOGL,META"

OPTIONS_URLS = (
    "https://query2.finance.yahoo.com/v7/finance/options/{symbol}",
    "https://query1.finance.yahoo.com/v7/finance/options/{symbol}",
)
COOKIE_URL = "https://fc.yahoo.com/"
CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"

TIMEOUT = 20.0
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_PAUSE = 0.25
# Yahoo throttles by IP (429 across all query* hosts at once); one spaced
# retry per URL is polite, anything more just extends the ban.
_RETRY_BACKOFF = 2.0

RISK_FREE_RATE = 0.04
PROB_FLOOR = 0.005
PROB_CAP = 0.995
CLOSE_HOUR_UTC = 21

MAX_ROWS = 60
MAX_EXPIRIES_PER_SYMBOL = 2
MAX_STRIKES_PER_EXPIRY = 10
NEAREST_STRIKES = 5
ROUND_BAND = 0.08


def watchlist() -> list[str]:
    raw = os.environ.get("FORECAST_EQUITY_WATCHLIST") or DEFAULT_WATCHLIST
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


# ── math ─────────────────────────────────────────────────────────────────────


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(spot: float, strike: float, iv: float, t_years: float, r: float = RISK_FREE_RATE) -> float | None:
    """Risk-neutral P(S_T > K) = N(d2). Clamped to [0.005, 0.995]."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return None
    d2 = (math.log(spot / strike) + (r - 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    return round(min(PROB_CAP, max(PROB_FLOOR, norm_cdf(d2))), 4)


# ── venue_id codec (also used by resolver.py) ────────────────────────────────


def _fmt_strike(k: float) -> str:
    return str(int(k)) if float(k).is_integer() else f"{k:g}"


def make_venue_id(symbol: str, expiry: date, strike: float) -> str:
    return f"{symbol}-{expiry.strftime('%Y%m%d')}-C{_fmt_strike(strike)}"


def parse_venue_id(venue_id: str) -> tuple[str, date, float] | None:
    """'SPY-20260814-C630' -> ('SPY', date(2026,8,14), 630.0); None if malformed.
    Symbol is joined from the leading parts so tickers containing '-' survive."""
    parts = (venue_id or "").split("-")
    if len(parts) < 3 or not parts[-1].startswith("C"):
        return None
    symbol = "-".join(parts[:-2])
    if not symbol:
        return None
    try:
        strike = float(parts[-1][1:])
        expiry = datetime.strptime(parts[-2], "%Y%m%d").date()
    except ValueError:
        return None
    return symbol, expiry, strike


# ── chain -> rows ────────────────────────────────────────────────────────────


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iv_ok(v) -> bool:
    # Yahoo encodes "no IV" as ~1e-5; anything under 1% (or over 500%) is junk.
    f = _to_float(v)
    return f is not None and 0.01 <= f <= 5.0


def _round_step(spot: float) -> float:
    if spot < 50:
        return 1.0
    if spot < 250:
        return 5.0
    if spot < 2000:
        return 10.0
    return 100.0


def _select_strikes(strikes: list[float], spot: float) -> list[float]:
    """~5 strikes nearest spot plus round-number strikes within +/-8% of spot,
    deduped, ordered nearest-first, capped."""
    uniq = sorted({k for k in strikes if k > 0})
    if not uniq or spot <= 0:
        return []
    by_dist = sorted(uniq, key=lambda k: (abs(k - spot), k))
    chosen = by_dist[:NEAREST_STRIKES]
    step = _round_step(spot)
    for k in uniq:
        if k not in chosen and abs(k / spot - 1.0) <= ROUND_BAND and (k / step).is_integer():
            chosen.append(k)
    chosen.sort(key=lambda k: (abs(k - spot), k))
    return chosen[:MAX_STRIKES_PER_EXPIRY]


def _liquidity(call: dict) -> float | None:
    oi = _to_float(call.get("openInterest"))
    bid = _to_float(call.get("bid"))
    ask = _to_float(call.get("ask"))
    if oi is not None and bid and ask and bid > 0 and ask > 0:
        return round(oi * 100.0 * (bid + ask) / 2.0, 2)
    return oi


def _expiry_rows(symbol: str, spot: float, expiry_epoch: int, calls: list[dict], now_dt: datetime) -> list[dict]:
    expiry = datetime.fromtimestamp(expiry_epoch, tz=timezone.utc).date()
    end_dt = datetime(expiry.year, expiry.month, expiry.day, CLOSE_HOUR_UTC, tzinfo=timezone.utc)
    t_years = (end_dt - now_dt).total_seconds() / (365.0 * 86400.0)
    if t_years <= 0:
        return []

    by_strike: dict[float, dict] = {}
    for c in calls:
        if not isinstance(c, dict):
            continue
        k = _to_float(c.get("strike"))
        if k is not None and k > 0:
            by_strike[k] = c
    valid_ivs = [(k, float(c["impliedVolatility"])) for k, c in by_strike.items() if _iv_ok(c.get("impliedVolatility"))]

    rows: list[dict] = []
    for k in _select_strikes(list(by_strike), spot):
        call = by_strike[k]
        if _iv_ok(call.get("impliedVolatility")):
            iv = float(call["impliedVolatility"])
        elif valid_ivs:
            iv = min(valid_ivs, key=lambda kv: abs(kv[0] - k))[1]
        else:
            continue
        prob = prob_above(spot, k, iv, t_years)
        if prob is None:
            continue
        rows.append(
            {
                "venue": "options",
                "venue_id": make_venue_id(symbol, expiry, k),
                "event_ticker": f"{symbol}-{expiry.strftime('%Y%m%d')}",
                "question": f"{symbol} closes at or above ${_fmt_strike(k)} on {expiry.isoformat()} (options-implied)",
                "category": "finance",
                "url": f"https://finance.yahoo.com/quote/{symbol}/options",
                "end_date": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "yes_bid": None,
                "yes_ask": None,
                "last_price": prob,
                "liquidity": _liquidity(call),
                "volume_24h": _to_float(call.get("volume")),
                "yes_token_id": None,
                "no_token_id": None,
            }
        )
    return rows


# ── Yahoo fetch ──────────────────────────────────────────────────────────────


async def _get_crumb(client: httpx.AsyncClient) -> str | None:
    # fc.yahoo.com 404s but sets the session cookie the crumb endpoint needs.
    try:
        await client.get(COOKIE_URL)
        resp = await client.get(CRUMB_URL)
        if resp.status_code == 200 and resp.text and "<" not in resp.text:
            return resp.text.strip()
    except Exception as e:
        log.warning("options: crumb fetch failed: %s", e)
    return None


async def _fetch_chain(client: httpx.AsyncClient, symbol: str, state: dict, date_epoch: int | None = None) -> dict | None:
    """Return optionChain.result[0] for the symbol (specific expiry when
    date_epoch given), or None. `state` caches the crumb across calls."""
    params: dict = {}
    if date_epoch is not None:
        params["date"] = int(date_epoch)
    if state.get("crumb"):
        params["crumb"] = state["crumb"]
    for url_t in OPTIONS_URLS:
        url = url_t.format(symbol=symbol)
        resp = None
        for attempt in (0, 1):
            try:
                resp = await client.get(url, params=params)
            except Exception:
                resp = None
                break
            if resp.status_code in (429, 500, 502, 503) and attempt == 0:
                await asyncio.sleep(_RETRY_BACKOFF)
                continue
            break
        if resp is None:
            continue
        if resp.status_code in (401, 403) and not state.get("crumb_tried"):
            state["crumb_tried"] = True
            crumb = await _get_crumb(client)
            if crumb:
                state["crumb"] = crumb
                params["crumb"] = crumb
                try:
                    resp = await client.get(url, params=params)
                except Exception:
                    continue
        if resp.status_code != 200:
            continue
        try:
            payload = resp.json()
        except ValueError:
            continue
        results = ((payload or {}).get("optionChain") or {}).get("result") or []
        if results and isinstance(results[0], dict):
            return results[0]
    return None


async def _symbol_rows(client: httpx.AsyncClient, symbol: str, state: dict, now_ts: float, max_ts: float) -> list[dict]:
    chain = await _fetch_chain(client, symbol, state)
    if chain is None:
        log.warning("options: no chain for %s; skipping", symbol)
        return []
    spot = _to_float((chain.get("quote") or {}).get("regularMarketPrice"))
    if spot is None or spot <= 0:
        log.warning("options: no spot for %s; skipping", symbol)
        return []
    expirations = sorted(int(e) for e in chain.get("expirationDates") or [] if isinstance(e, (int, float)))
    inside = [e for e in expirations if now_ts < e <= max_ts]
    selected = inside[:MAX_EXPIRIES_PER_SYMBOL]
    if not selected:
        selected = [e for e in expirations if e > now_ts][:1]
    if not selected:
        return []

    base_opts = ((chain.get("options") or [{}])[0]) or {}
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    rows: list[dict] = []
    for epoch in selected:
        if int(base_opts.get("expirationDate") or -1) == epoch:
            calls = base_opts.get("calls") or []
        else:
            sub = await _fetch_chain(client, symbol, state, date_epoch=epoch)
            calls = ((((sub or {}).get("options") or [{}])[0]) or {}).get("calls") or []
            await asyncio.sleep(_PAUSE)
        rows.extend(_expiry_rows(symbol, spot, epoch, calls, now_dt))
    return rows


def _round_robin(buckets: list[list[dict]], cap: int) -> list[dict]:
    """Interleave per-symbol rows so the cap trims every symbol evenly instead
    of dropping the tail of the watchlist. Buckets are nearest-strike-first."""
    out: list[dict] = []
    i = 0
    while len(out) < cap:
        advanced = False
        for b in buckets:
            if i < len(b):
                out.append(b[i])
                advanced = True
                if len(out) >= cap:
                    break
        if not advanced:
            break
        i += 1
    return out


async def fetch_horizon(horizon_days: int) -> list[dict]:
    """Fetch options-implied markets for the watchlist within `horizon_days`.

    Returns a list of normalized market dicts (see module docstring).
    Never raises — failing symbols are skipped (logged once each).
    """
    out: list[dict] = []
    try:
        try:
            days = max(1, int(horizon_days))
        except (TypeError, ValueError):
            days = 7
        now_ts = time.time()
        max_ts = now_ts + days * 86400
        state: dict = {}
        buckets: list[list[dict]] = []
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"User-Agent": _UA, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            for symbol in watchlist():
                try:
                    rows = await _symbol_rows(client, symbol, state, now_ts, max_ts)
                except Exception as e:
                    log.warning("options: %s failed: %s", symbol, e)
                    rows = []
                if rows:
                    buckets.append(rows)
                await asyncio.sleep(_PAUSE)
        out = _round_robin(buckets, MAX_ROWS)
    except Exception as e:
        log.error("options ingest failed: %s", e)
    return out
