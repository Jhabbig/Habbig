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
- When Yahoo is unavailable (its per-IP 429 bans last days), chains fall
  back to CBOE's keyless delayed-quotes JSON, which returns every expiry in
  one response with per-contract iv/open_interest/bid/ask. Contracts are
  OCC-coded (AAPL260814C00110000); only roots that exactly match the symbol
  are used, so adjusted/weekly roots never masquerade as the underlying.
- r = 0.04 constant: a short-rate proxy — at day-to-week horizons the rate
  term moves d2 by well under a probability point, so precision is wasted.
- end_date = expiry date 21:00:00Z approximates the 16:00 ET close (exact
  under DST, off by one hour in winter — immaterial at these horizons).
- All model inputs (spot S, strike K, contract IV, T, r) are recoverable:
  K and expiry live in the venue_id, S/IV from the venue at expiry.
- Output is bounded: <=2 expiries per symbol, each contributing <=10 ladder
  strikes plus one direction (D) row anchored at the previous close; a
  global cap of 60 rows is applied round-robin across symbols, with
  direction rows leading each bucket so the cap can never trim them.
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
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"

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

THROTTLE_COOLDOWN_S = 3600
# Single-element list so tests can reset it; module-level because the ban is
# per-IP and outlives any one fetch_horizon call.
_throttled_until = [0.0]


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


def make_venue_id(symbol: str, expiry: date, strike: float, kind: str = "C") -> str:
    return f"{symbol}-{expiry.strftime('%Y%m%d')}-{kind}{_fmt_strike(strike)}"


def parse_venue_id(venue_id: str) -> tuple[str, date, float] | None:
    """'SPY-20260814-C630' -> ('SPY', date(2026,8,14), 630.0); None if malformed.
    Accepts kind C (strike ladder, settles close >= K) and D (direction row
    anchored at previous close, settles close > K — see is_direction).
    Symbol is joined from the leading parts so tickers containing '-' survive."""
    parts = (venue_id or "").split("-")
    if len(parts) < 3 or not parts[-1][:1] in ("C", "D"):
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


def is_direction(venue_id: str) -> bool:
    parts = (venue_id or "").split("-")
    return bool(parts) and parts[-1].startswith("D")


def parse_occ(occ: str, symbol: str) -> tuple[date, str, float] | None:
    """'AAPL260814C00110000' -> (date(2026,8,14), 'C', 110.0) for symbol AAPL.

    The root must equal the symbol exactly — adjusted contracts (AAPL1) and
    other roots on the same underlying are rejected rather than mispriced.
    Strike is OCC dollars*1000 in the trailing 8 digits."""
    if not occ or not occ.startswith(symbol):
        return None
    body = occ[len(symbol) :]
    if len(body) != 15 or body[6] not in ("C", "P"):
        return None
    try:
        expiry = datetime.strptime(body[:6], "%y%m%d").date()
        strike = int(body[7:]) / 1000.0
    except ValueError:
        return None
    return expiry, body[6], strike


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


def _expiry_rows(symbol: str, spot: float, expiry_epoch: int, calls: list[dict], now_dt: datetime, ref: float | None = None) -> list[dict]:
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

    # Direction row: P(ends UP vs the previous close) at this expiry, priced
    # off the nearest-strike IV. The anchor is the PREVIOUS CLOSE, not the
    # live spot — spot moves every ingest cycle and an id keyed on it would
    # mint a new near-duplicate market per tick; prev close is stable for a
    # whole trading day, so each day asks exactly one genuine question.
    # Kind 'D' settles strictly greater in the resolver ("ends UP" excludes
    # a flat close).
    if valid_ivs and ref is not None and ref > 0:
        atm_iv = min(valid_ivs, key=lambda kv: abs(kv[0] - spot))[1]
        anchor = round(ref, 2)
        prob_up = prob_above(spot, anchor, atm_iv, t_years)
        if prob_up is not None:
            exp_move = atm_iv * math.sqrt(t_years)
            rows.append(
                {
                    "venue": "options",
                    "venue_id": make_venue_id(symbol, expiry, anchor, kind="D"),
                    "event_ticker": f"{symbol}-{expiry.strftime('%Y%m%d')}",
                    "question": (f"{symbol} ends UP vs prev close (${_fmt_strike(anchor)}) on {expiry.isoformat()} (options-implied; expected move ±{exp_move:.1%})"),
                    "category": "finance",
                    "url": f"https://finance.yahoo.com/quote/{symbol}/options",
                    "end_date": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "yes_bid": None,
                    "yes_ask": None,
                    "last_price": prob_up,
                    "liquidity": _liquidity(by_strike[min(by_strike, key=lambda k: abs(k - spot))]),
                    "volume_24h": None,
                    "yes_token_id": None,
                    "no_token_id": None,
                }
            )

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


# ── CBOE fallback ────────────────────────────────────────────────────────────


async def _cboe_symbol_rows(client: httpx.AsyncClient, symbol: str, now_ts: float, max_ts: float) -> list[dict]:
    """Chain rows from CBOE delayed quotes — every expiry arrives in one
    response, so this is a single request per symbol."""
    try:
        resp = await client.get(CBOE_URL.format(symbol=symbol))
        if resp.status_code != 200:
            return []
        data = (resp.json() or {}).get("data") or {}
    except Exception as e:
        log.warning("options: CBOE fetch failed for %s: %s", symbol, e)
        return []
    spot = _to_float(data.get("current_price"))
    if spot is None or spot <= 0:
        return []
    ref = _to_float(data.get("prev_day_close")) or spot

    by_expiry: dict[date, list[dict]] = {}
    for o in data.get("options") or []:
        if not isinstance(o, dict):
            continue
        parsed = parse_occ(o.get("option") or "", symbol)
        if not parsed:
            continue
        expiry, kind, strike = parsed
        if kind != "C":
            continue
        by_expiry.setdefault(expiry, []).append(
            {
                "strike": strike,
                "impliedVolatility": o.get("iv"),
                "openInterest": o.get("open_interest"),
                "bid": o.get("bid"),
                "ask": o.get("ask"),
                "volume": o.get("volume"),
            }
        )
    if not by_expiry:
        return []

    epochs = {e: int(datetime(e.year, e.month, e.day, tzinfo=timezone.utc).timestamp()) for e in by_expiry}
    inside = sorted(e for e in by_expiry if now_ts < epochs[e] <= max_ts)
    selected = inside[:MAX_EXPIRIES_PER_SYMBOL]
    if not selected:
        selected = sorted(e for e in by_expiry if epochs[e] > now_ts)[:1]

    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    rows: list[dict] = []
    for e in selected:
        rows.extend(_expiry_rows(symbol, spot, epochs[e], by_expiry[e], now_dt, ref=ref))
    rows.sort(key=lambda r: 0 if is_direction(r["venue_id"]) else 1)
    if rows:
        log.info("options: %s served from CBOE fallback (%d rows)", symbol, len(rows))
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
    if time.time() < _throttled_until[0]:
        return None
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
            if resp.status_code == 429:
                # Yahoo bans by IP and every request while banned extends it.
                # Go quiet for a full hour instead of re-poking each cycle.
                _throttled_until[0] = time.time() + THROTTLE_COOLDOWN_S
                log.warning("options: Yahoo 429 — backing off for %d min", THROTTLE_COOLDOWN_S // 60)
                return None
            if resp.status_code in (500, 502, 503) and attempt == 0:
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
        rows = await _cboe_symbol_rows(client, symbol, now_ts, max_ts)
        if not rows:
            log.warning("options: no chain for %s (yahoo+cboe); skipping", symbol)
        return rows
    quote = chain.get("quote") or {}
    spot = _to_float(quote.get("regularMarketPrice"))
    if spot is None or spot <= 0:
        log.warning("options: no spot for %s; skipping", symbol)
        return []
    # Direction anchor: previous close when the venue provides it, else the
    # current spot (still stable enough intraday to be a fair anchor).
    ref = _to_float(quote.get("regularMarketPreviousClose")) or spot
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
        rows.extend(_expiry_rows(symbol, spot, epoch, calls, now_dt, ref=ref))
    # Direction rows lead the bucket so the global row cap can never trim
    # them in favor of deep ladder strikes.
    rows.sort(key=lambda r: 0 if is_direction(r["venue_id"]) else 1)
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
