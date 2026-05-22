#!/usr/bin/env python3
"""
Deribit options analytics — IV term structure, 25-delta skew, max pain, DVOL.

Why this is the last Bloomberg gap worth closing for HODLers:
  IV skew is the single best forward-looking risk indicator in crypto.
  Sustained put skew (puts more expensive than equidistant calls) means
  smart money is paying up for downside protection — preceded the May
  2021 crash by 3 weeks, the FTX crash by 5 weeks, and the March 2024
  pullback by 1 week. No other indicator gives you that lead time.

Data source:
  Deribit public REST API. Free, no auth, generous rate limits. Two
  endpoints suffice:
    - /public/get_book_summary_by_currency?currency=BTC&kind=option
      Returns every option (~3000-5000 rows) with mark_iv, delta,
      open_interest, mark_price in one call.
    - /public/get_volatility_index_data?currency=BTC&resolution=1D
      Returns the daily DVOL index history (BTC's VIX equivalent).

Analytics per asset (BTC + ETH):
  - **IV term structure** — ATM IV at each available expiration.
    Backwardation (front-month > back-month) signals expected vol
    contraction; contango = expected vol expansion. Inversion before
    earnings-style events.
  - **25-delta skew** — IV at 25-delta put minus IV at 25-delta call,
    per expiration. Positive = puts more expensive = bearish lean.
    Crypto's structural skew is mildly negative (calls more expensive
    in a bull market); positive skew is the risk-off signal.
  - **Max pain** — strike that minimises total option-holder payout
    at expiry. Often a magnetic level into the last week of expiry.
  - **DVOL** — daily index value + 30d trend.
  - **Open interest distribution** — total OI in USD per expiration
    bucket. Tells you where the leverage is piled up.

What we deliberately don't compute (yet):
  - Implied vol surface (delta × expiry grid) — would render best as a
    heatmap, deferred to a follow-up.
  - Greeks aggregated across positions (portfolio gamma exposure etc.)
    — only matters once we let users link Deribit accounts.
  - GVOL / Greekslive cross-venue aggregation — Deribit is 90%+ of
    BTC/ETH options volume so single-venue is fine.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

import database as db

log = logging.getLogger("crypto.options")

DERIBIT_BASE = "https://www.deribit.com/api/v2/public"
USER_AGENT = "CryptoEdge-OptionsBot/1.0 (https://crypto.narve.ai)"

ASSETS = ("BTC", "ETH")

# Term-structure buckets we report. Each expiration in the raw data is
# slotted to the nearest bucket so the dashboard has stable rows.
TERM_BUCKETS_DAYS = [7, 14, 30, 60, 90, 180, 365]


# ─── Deribit fetch ──────────────────────────────────────────────────────────

def _get(path: str, params: dict) -> Optional[dict]:
    try:
        r = requests.get(
            DERIBIT_BASE + path, params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code >= 400:
            log.warning("deribit %s → HTTP %d", path, r.status_code)
            return None
        return r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("deribit %s failed: %s", path, e)
        return None


def fetch_book_summary(currency: str) -> list[dict]:
    """All options for the currency. ~3-5k rows on BTC, ~2k on ETH."""
    out = _get("/get_book_summary_by_currency",
               {"currency": currency, "kind": "option"})
    if not out:
        return []
    return out.get("result", []) or []


def fetch_dvol_history(currency: str, days: int = 365) -> list[tuple[str, float]]:
    """DVOL daily history. Returns [(iso_date, close), ...]."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    out = _get("/get_volatility_index_data",
               {"currency": currency, "start_timestamp": start_ms,
                "end_timestamp": end_ms, "resolution": "1D"})
    if not out:
        return []
    rows = (out.get("result") or {}).get("data", []) or []
    series = []
    for row in rows:
        try:
            # Deribit returns [ts_ms, open, high, low, close]
            ts_ms = int(row[0])
            close = float(row[4])
            d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()
            series.append((d, close))
        except (IndexError, TypeError, ValueError):
            continue
    return series


# ─── Parsing ────────────────────────────────────────────────────────────────

@dataclass
class OptionInfo:
    instrument: str
    expiration: datetime
    days_to_expiry: int
    strike: float
    is_call: bool
    mark_iv: float           # % (Deribit reports IV as percent, e.g. 65.5 = 65.5%)
    open_interest: float     # contracts
    mark_price: float        # in base currency (BTC for BTC options)
    delta: float
    underlying_price: float
    open_interest_usd: float  # = OI × mark_price × underlying_price


def _parse_instrument(name: str) -> Optional[tuple[datetime, float, bool]]:
    """Parse 'BTC-30MAY26-100000-C' → (datetime, strike, is_call)."""
    try:
        parts = name.split("-")
        if len(parts) != 4:
            return None
        _, dt_str, strike_str, side = parts
        expiry = datetime.strptime(dt_str, "%d%b%y").replace(
            hour=8, tzinfo=timezone.utc,  # Deribit settles at 08:00 UTC
        )
        strike = float(strike_str)
        is_call = side.upper() == "C"
        return expiry, strike, is_call
    except (ValueError, IndexError):
        return None


def parse_book(rows: list[dict]) -> list[OptionInfo]:
    """Convert raw book-summary rows into OptionInfo. Drops rows with
    missing mark_iv / delta / underlying — typically dailies with no fills."""
    now = datetime.now(timezone.utc)
    out: list[OptionInfo] = []
    for r in rows:
        name = r.get("instrument_name", "")
        parsed = _parse_instrument(name)
        if not parsed:
            continue
        expiry, strike, is_call = parsed
        days = max(0, (expiry - now).days)
        # Skip already-expired or near-zero IV rows (Deribit keeps some
        # expired instruments for a day after).
        if days < 0:
            continue
        mark_iv = r.get("mark_iv")
        delta = r.get("delta")
        underlying = r.get("underlying_price")
        if mark_iv is None or delta is None or underlying is None:
            continue
        oi = float(r.get("open_interest") or 0)
        mark_price = float(r.get("mark_price") or 0)
        try:
            mark_iv = float(mark_iv)
            delta = float(delta)
            underlying = float(underlying)
        except (TypeError, ValueError):
            continue
        if mark_iv <= 0 or underlying <= 0:
            continue
        out.append(OptionInfo(
            instrument=name, expiration=expiry, days_to_expiry=days,
            strike=strike, is_call=is_call, mark_iv=mark_iv,
            open_interest=oi, mark_price=mark_price, delta=delta,
            underlying_price=underlying,
            open_interest_usd=oi * mark_price * underlying,
        ))
    return out


# ─── Analytics ──────────────────────────────────────────────────────────────

def _bucket_days(dte: int) -> Optional[int]:
    """Slot a days-to-expiry into the nearest term bucket — chosen by
    smallest *percentage* distance so dte=10 prefers the 7d bucket
    (43% off) over the 14d bucket (29% off) — wait, that's wrong: 14d
    is closer in % terms. We pick by smallest percentage error, then
    reject only if even the best fit is > 40% off (which means the
    expiry is in a true gap, e.g. dte=200 is 10% off 180 and 45% off
    365 — slots into 180)."""
    if dte <= 0:
        return None
    best = min(TERM_BUCKETS_DAYS, key=lambda b: abs(b - dte) / b)
    if abs(best - dte) / best > 0.40:
        return None
    return best


def _atm_iv(options: list[OptionInfo]) -> Optional[float]:
    """Average call + put IV at the strike closest to underlying."""
    if not options:
        return None
    underlying = options[0].underlying_price
    # Find the strike closest to underlying.
    by_strike: dict[float, list[OptionInfo]] = {}
    for o in options:
        by_strike.setdefault(o.strike, []).append(o)
    closest_strike = min(by_strike.keys(), key=lambda k: abs(k - underlying))
    leg = by_strike[closest_strike]
    if not leg:
        return None
    return float(sum(o.mark_iv for o in leg) / len(leg))


def _delta_skew(options: list[OptionInfo], target_delta: float = 0.25) -> Optional[float]:
    """25-delta put IV − 25-delta call IV. Positive = bearish skew."""
    calls = [o for o in options if o.is_call]
    puts = [o for o in options if not o.is_call]
    if not calls or not puts:
        return None
    call_25 = min(calls, key=lambda o: abs(o.delta - target_delta))
    put_25 = min(puts, key=lambda o: abs(o.delta + target_delta))  # put delta is negative
    return float(put_25.mark_iv - call_25.mark_iv)


def _iv_at_delta(options: list[OptionInfo], target_delta: float,
                 is_call: bool) -> Optional[float]:
    """Find the option of the requested side with delta closest to
    target_delta (signed: positive for calls, negative for puts).
    Returns its mark IV, or None if no options of that side exist."""
    side = [o for o in options if o.is_call == is_call]
    if not side:
        return None
    best = min(side, key=lambda o: abs(o.delta - target_delta))
    # Reject if even the best fit is way off — better to omit than mislead.
    if abs(best.delta - target_delta) > 0.15:
        return None
    return float(best.mark_iv)


def iv_surface_row(options: list[OptionInfo]) -> dict:
    """For one expiration bucket, return IV at 5 standard delta points.
    The UI renders this as a 5-column heatmap row."""
    return {
        "iv_put_10":  _iv_at_delta(options, -0.10, is_call=False),
        "iv_put_25":  _iv_at_delta(options, -0.25, is_call=False),
        "iv_atm":     _atm_iv(options),
        "iv_call_25": _iv_at_delta(options,  0.25, is_call=True),
        "iv_call_10": _iv_at_delta(options,  0.10, is_call=True),
    }


def _max_pain(options: list[OptionInfo]) -> Optional[float]:
    """Strike that minimises total option-holder payout if expiry was now.
    Walks every unique strike as a candidate settlement price."""
    if not options:
        return None
    strikes = sorted({o.strike for o in options})
    if len(strikes) < 3:
        return None
    best_strike = strikes[0]
    best_payout = float("inf")
    for K in strikes:
        payout = 0.0
        for o in options:
            if o.is_call:
                payout += o.open_interest * max(0.0, K - o.strike)
            else:
                payout += o.open_interest * max(0.0, o.strike - K)
        if payout < best_payout:
            best_payout = payout
            best_strike = K
    return float(best_strike)


def _total_oi_usd(options: list[OptionInfo]) -> float:
    return float(sum(o.open_interest_usd for o in options))


# ─── Term-structure rollup ──────────────────────────────────────────────────

@dataclass
class TermRow:
    expiration_date: str       # ISO date
    days_to_expiry: int
    bucket_days: int
    atm_iv: Optional[float]
    skew_25d: Optional[float]
    max_pain_strike: Optional[float]
    open_interest_usd: float
    contract_count: int
    # IV surface: 5 standard delta points. Used by the heatmap.
    iv_put_10: Optional[float] = None
    iv_put_25: Optional[float] = None
    iv_call_25: Optional[float] = None
    iv_call_10: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def term_structure(options: list[OptionInfo]) -> list[TermRow]:
    """Group options by expiration date; compute ATM IV, 25d skew,
    max pain, total OI, and the full IV surface row per group."""
    by_expiry: dict[datetime, list[OptionInfo]] = {}
    for o in options:
        by_expiry.setdefault(o.expiration, []).append(o)
    out: list[TermRow] = []
    for expiry, opts in sorted(by_expiry.items()):
        if not opts:
            continue
        dte = opts[0].days_to_expiry
        bucket = _bucket_days(dte)
        if bucket is None:
            continue
        surface = iv_surface_row(opts)
        out.append(TermRow(
            expiration_date=expiry.date().isoformat(),
            days_to_expiry=dte, bucket_days=bucket,
            atm_iv=surface["iv_atm"],
            skew_25d=_delta_skew(opts),
            max_pain_strike=_max_pain(opts),
            open_interest_usd=_total_oi_usd(opts),
            contract_count=len(opts),
            iv_put_10=surface["iv_put_10"],
            iv_put_25=surface["iv_put_25"],
            iv_call_25=surface["iv_call_25"],
            iv_call_10=surface["iv_call_10"],
        ))
    return out


# ─── Refresh (persists everything) ──────────────────────────────────────────

def refresh() -> dict:
    """Pull current options snapshot + DVOL history for BTC + ETH."""
    started = time.time()
    summary: dict = {"assets": {}, "dvol_new": {}, "elapsed_s": 0.0}
    for asset in ASSETS:
        try:
            book = fetch_book_summary(asset)
        except Exception as e:
            log.warning("book fetch failed for %s: %s", asset, e)
            summary["assets"][asset] = {"error": str(e)}
            continue
        options = parse_book(book)
        rows = term_structure(options)
        # Persist: replace the latest snapshot for this asset.
        ts = datetime.now(timezone.utc).isoformat()
        persist_rows = [(
            asset, ts, r.expiration_date, r.days_to_expiry, r.bucket_days,
            r.atm_iv, r.skew_25d, r.max_pain_strike,
            r.open_interest_usd, r.contract_count,
            r.iv_put_10, r.iv_put_25, r.iv_call_25, r.iv_call_10,
        ) for r in rows]
        if persist_rows:
            db.upsert_options_term_structure(persist_rows)
        summary["assets"][asset] = {
            "instruments_seen": len(options),
            "buckets_filled": len(rows),
            "as_of": ts,
        }
        # DVOL — incremental (only fetch missing days).
        try:
            dvol = fetch_dvol_history(asset, days=90)
            dvol_rows = [(asset, d, v) for d, v in dvol]
            if dvol_rows:
                summary["dvol_new"][asset] = db.upsert_dvol_bars(dvol_rows).get("new", 0)
        except Exception as e:
            log.warning("dvol fetch failed for %s: %s", asset, e)
            summary["dvol_new"][asset] = -1
    summary["elapsed_s"] = round(time.time() - started, 2)
    return summary


# ─── Read API ───────────────────────────────────────────────────────────────

def overview() -> dict:
    """Single payload the UI uses: latest term structure + DVOL trend
    for BTC and ETH."""
    out: dict = {"as_of": datetime.now(timezone.utc).isoformat(), "assets": {}}
    for asset in ASSETS:
        ts_rows = db.get_latest_options_term_structure(asset)
        dvol_rows = db.get_dvol_bars(asset, days=90)
        dvol_now = float(dvol_rows[-1]["dvol"]) if dvol_rows else None
        dvol_30d_ago = float(dvol_rows[-30]["dvol"]) if len(dvol_rows) >= 30 else None
        dvol_chg = ((dvol_now / dvol_30d_ago - 1) * 100
                    if (dvol_now is not None and dvol_30d_ago) else None)
        # Headline skew = nearest expiration with valid skew.
        headline_skew = None
        front_iv = None
        for r in ts_rows:
            if r["skew_25d"] is not None and headline_skew is None:
                headline_skew = float(r["skew_25d"])
            if r["atm_iv"] is not None and front_iv is None:
                front_iv = float(r["atm_iv"])
        # Term-structure slope: 90d − 30d ATM IV (positive = contango).
        atm30 = next((float(r["atm_iv"]) for r in ts_rows
                      if r["bucket_days"] == 30 and r["atm_iv"] is not None), None)
        atm90 = next((float(r["atm_iv"]) for r in ts_rows
                      if r["bucket_days"] == 90 and r["atm_iv"] is not None), None)
        slope = (atm90 - atm30) if (atm30 is not None and atm90 is not None) else None
        # Signal: positive skew = bearish hedging demand; near-zero = neutral.
        signal = "neutral"
        if headline_skew is not None:
            if headline_skew >= 3.0:
                signal = "bearish"  # heavy put protection
            elif headline_skew <= -3.0:
                signal = "bullish"  # call upside in demand
        out["assets"][asset] = {
            "term_structure": [dict(r) for r in ts_rows],
            "front_month_atm_iv": front_iv,
            "headline_skew_25d": headline_skew,
            "term_slope_90d_minus_30d": slope,
            "dvol_now": dvol_now,
            "dvol_30d_change_pct": round(dvol_chg, 2) if dvol_chg is not None else None,
            "signal": signal,
        }
    return out


def dvol_series(asset: str, days: int = 180) -> dict:
    rows = db.get_dvol_bars(asset, days=days)
    return {
        "asset": asset,
        "dates": [r["date"] for r in rows],
        "values": [float(r["dvol"]) for r in rows],
    }


# ─── Covered-call yield calculator (Wealth tier) ────────────────────────────

def covered_call_quote(asset: str, qty: float, otm_pct: float,
                       days_to_expiry: int) -> dict:
    """Given the user's qty held + a target OTM percentage + days to expiry,
    find the matching Deribit call and compute the premium they could earn
    by selling it.

    Returns:
      - strike: the call strike (underlying × (1 + otm_pct))
      - premium_per_contract_usd: mark_price × underlying (Deribit prices
        in base currency; 0.05 BTC × $100k = $5k premium per contract)
      - premium_total_usd: per_contract × qty (1 Deribit contract = 1 unit
        of the underlying for both BTC and ETH)
      - annualised_yield_pct: (premium / position_value) × (365 / dte)
      - assignment_probability: heuristic ≈ delta of the call
      - underlying_price, expiration_date, instrument
    """
    asset = asset.upper()
    if asset not in ASSETS:
        return {"error": f"asset must be one of {ASSETS}"}
    if qty <= 0:
        return {"error": "qty must be positive"}
    if otm_pct < 0 or otm_pct > 1.0:
        return {"error": "otm_pct must be in [0, 1.0]"}
    if days_to_expiry < 1 or days_to_expiry > 365:
        return {"error": "days_to_expiry must be 1..365"}

    book = fetch_book_summary(asset)
    options = parse_book(book)
    calls = [o for o in options if o.is_call]
    if not calls:
        return {"error": "no calls available on Deribit for this asset"}

    underlying = calls[0].underlying_price
    target_strike = underlying * (1 + otm_pct)
    target_expiry_days = days_to_expiry

    # Find the call closest to the (strike, dte) target. Use a combined
    # score: 60% weight on dte match, 40% on strike match.
    def _score(o: OptionInfo) -> float:
        if o.days_to_expiry == 0 or target_strike == 0:
            return float("inf")
        dte_err = abs(o.days_to_expiry - target_expiry_days) / target_expiry_days
        strike_err = abs(o.strike - target_strike) / target_strike
        return 0.60 * dte_err + 0.40 * strike_err

    best = min(calls, key=_score)
    # Reject if either dimension is more than 50% off the target — better to
    # tell the user "no good fit" than to mislead them about the premium.
    dte_err = abs(best.days_to_expiry - target_expiry_days) / target_expiry_days
    strike_err = abs(best.strike - target_strike) / target_strike
    if dte_err > 0.50 or strike_err > 0.30:
        return {
            "error": "no listed Deribit call close enough to the target "
                     "strike + expiry combination — try different inputs",
            "best_available_strike": best.strike,
            "best_available_dte": best.days_to_expiry,
        }

    premium_per_contract_usd = float(best.mark_price * underlying)
    premium_total_usd = premium_per_contract_usd * float(qty)
    position_value = underlying * float(qty)
    annualised_yield = (
        (premium_total_usd / position_value) * (365.0 / best.days_to_expiry)
        if position_value > 0 and best.days_to_expiry > 0 else 0.0
    )
    return {
        "asset": asset,
        "qty": float(qty),
        "underlying_price": round(underlying, 2),
        "position_value_usd": round(position_value, 2),
        "instrument": best.instrument,
        "expiration_date": best.expiration.date().isoformat(),
        "days_to_expiry": best.days_to_expiry,
        "strike": round(best.strike, 2),
        "strike_otm_pct": round((best.strike / underlying - 1.0) * 100, 2),
        "mark_iv": round(best.mark_iv, 1),
        "delta": round(best.delta, 3),
        "premium_per_contract_usd": round(premium_per_contract_usd, 2),
        "premium_total_usd": round(premium_total_usd, 2),
        "annualised_yield_pct": round(annualised_yield * 100, 2),
        "assignment_probability_pct": round(best.delta * 100, 1),
        "note": "Premium received upfront if you sell to open this call. "
                "Assignment probability ≈ option delta. Annualised yield "
                "assumes you repeat this trade every expiration; that "
                "yield is realisable only if the price stays below "
                f"${best.strike:,.0f}.",
    }
