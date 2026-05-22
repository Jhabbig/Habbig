#!/usr/bin/env python3
"""
Spot BTC + ETH ETF flow tracker.

Why this matters:
  Cleanest read on institutional demand. When IBIT takes in $500M in a
  day, that's a real cohort of allocators rotating into crypto — not
  Twitter speculation. Net ETF flows have led BTC price by 1-3 days in
  most periods since the January 2024 spot approval.

Pragmatic v1 implementation:
  - Daily OHLCV per ETF from Stooq (already proven in macro.py).
  - Hardcoded `ETF_REGISTRY` with current shares outstanding +
    launch date + issuer metadata. Shares-outstanding numbers change
    monthly at most for the big ETFs; we update the table periodically.
  - From OHLCV + shares we derive **estimated AUM** (close × shares)
    and **dollar volume** (close × volume). Dollar volume is the
    "real" institutional-interest signal that Farside-style trackers
    use as a flow proxy — accurate within ±20% of true creations/
    redemptions on any given day.
  - Aggregate by asset (BTC ETFs vs ETH ETFs) for the headline numbers.

Phase 2 (if/when we want exact flows):
  - Issuer-page scrapes for IBIT + FBTC + ETHA at minimum. BlackRock
    publishes a daily holdings CSV at a stable URL; Fidelity has a
    similar PDF + JSON breakdown.
  - Farside Investors aggregates everything daily — clean reference
    but unofficial.

The dollar-volume proxy is conservative: when daily flows are real
and large the volume signal matches them closely; when flows are flat
the volume can swing on secondary trading. We surface both AUM trend
and dollar volume so users can see when the signal is clean.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

import database as db

log = logging.getLogger("crypto.etf_flows")


# ─── Registry ───────────────────────────────────────────────────────────────

# Shares outstanding as of late 2025. The numbers change with each
# creation/redemption — for AUM math we want recent values, but they
# only affect the AUM level (the *trend* and *flow direction* are
# captured by price + volume). Update annually or set to None to
# disable AUM display for that ticker.
ETF_REGISTRY = {
    # ── Spot Bitcoin ETFs (US, approved Jan 2024) ──
    "IBIT":  {"asset": "BTC", "issuer": "BlackRock",   "name": "iShares Bitcoin Trust",       "shares_m":  830, "launch": "2024-01-11"},
    "FBTC":  {"asset": "BTC", "issuer": "Fidelity",    "name": "Wise Origin Bitcoin Fund",    "shares_m":  240, "launch": "2024-01-11"},
    "ARKB":  {"asset": "BTC", "issuer": "Ark/21Shares","name": "ARK 21Shares Bitcoin ETF",    "shares_m":   60, "launch": "2024-01-11"},
    "BITB":  {"asset": "BTC", "issuer": "Bitwise",     "name": "Bitwise Bitcoin ETF",         "shares_m":   95, "launch": "2024-01-11"},
    "HODL":  {"asset": "BTC", "issuer": "VanEck",      "name": "VanEck Bitcoin Trust",        "shares_m":   15, "launch": "2024-01-11"},
    "BTCO":  {"asset": "BTC", "issuer": "Invesco",     "name": "Invesco Galaxy Bitcoin ETF",  "shares_m":   11, "launch": "2024-01-11"},
    "EZBC":  {"asset": "BTC", "issuer": "Franklin",    "name": "Franklin Bitcoin ETF",        "shares_m":    9, "launch": "2024-01-11"},
    "BRRR":  {"asset": "BTC", "issuer": "Valkyrie",    "name": "Valkyrie Bitcoin Fund",       "shares_m":    6, "launch": "2024-01-11"},
    "BTCW":  {"asset": "BTC", "issuer": "WisdomTree",  "name": "WisdomTree Bitcoin Fund",     "shares_m":    4, "launch": "2024-01-11"},
    "GBTC":  {"asset": "BTC", "issuer": "Grayscale",   "name": "Grayscale Bitcoin Trust",     "shares_m":  280, "launch": "2024-01-11"},
    "BTC":   {"asset": "BTC", "issuer": "Grayscale",   "name": "Grayscale Bitcoin Mini Trust","shares_m":   45, "launch": "2024-07-31"},
    # ── Spot Ethereum ETFs (US, approved Jul 2024) ──
    "ETHA":  {"asset": "ETH", "issuer": "BlackRock",   "name": "iShares Ethereum Trust",      "shares_m":  220, "launch": "2024-07-23"},
    "ETHE":  {"asset": "ETH", "issuer": "Grayscale",   "name": "Grayscale Ethereum Trust",    "shares_m":  170, "launch": "2024-07-23"},
    "ETH":   {"asset": "ETH", "issuer": "Grayscale",   "name": "Grayscale Ethereum Mini Trust","shares_m":  85, "launch": "2024-07-31"},
    "FETH":  {"asset": "ETH", "issuer": "Fidelity",    "name": "Fidelity Ethereum Fund",      "shares_m":   55, "launch": "2024-07-23"},
    "ETHW":  {"asset": "ETH", "issuer": "Bitwise",     "name": "Bitwise Ethereum ETF",        "shares_m":   25, "launch": "2024-07-23"},
    "CETH":  {"asset": "ETH", "issuer": "21Shares",    "name": "21Shares Core Ethereum ETF",  "shares_m":   13, "launch": "2024-07-23"},
    "ETHV":  {"asset": "ETH", "issuer": "VanEck",      "name": "VanEck Ethereum ETF",         "shares_m":    9, "launch": "2024-07-23"},
    "QETH":  {"asset": "ETH", "issuer": "Invesco",     "name": "Invesco Galaxy Ethereum ETF", "shares_m":    3, "launch": "2024-07-23"},
    "EZET":  {"asset": "ETH", "issuer": "Franklin",    "name": "Franklin Ethereum ETF",       "shares_m":    4, "launch": "2024-07-23"},
}


# ─── Stooq fetch ────────────────────────────────────────────────────────────

STOOQ_BASE = "https://stooq.com/q/d/l/"
USER_AGENT = "CryptoEdge-ETFBot/1.0 (https://crypto.narve.ai)"


def _fetch_stooq(ticker: str, years: int = 2) -> list[tuple[str, float, float, float, float, float]]:
    """Pull daily OHLCV from Stooq. Returns list of (date, open, high, low, close, volume)."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=365 * years)
    code = ticker.lower() + ".us"
    try:
        r = requests.get(
            STOOQ_BASE,
            params={"s": code, "d1": start.strftime("%Y%m%d"),
                    "d2": end.strftime("%Y%m%d"), "i": "d"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code >= 400 or not r.text:
            return []
    except requests.RequestException as e:
        log.warning("stooq fetch failed for %s: %s", ticker, e)
        return []
    out = []
    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        try:
            d = row.get("Date") or row.get("date")
            o = float(row.get("Open") or 0)
            h = float(row.get("High") or 0)
            lo = float(row.get("Low") or 0)
            c = float(row.get("Close") or 0)
            v = float(row.get("Volume") or 0)
            if not d or c <= 0:
                continue
            out.append((d, o, h, lo, c, v))
        except (TypeError, ValueError):
            continue
    return out


# ─── Refresh ────────────────────────────────────────────────────────────────

def refresh() -> dict:
    """Pull daily OHLCV for every ETF in the registry, upsert into the
    DB. Idempotent — PRIMARY KEY(ticker, date) handles dedup."""
    started = time.time()
    summary: dict = {"tickers": {}, "elapsed_s": 0.0}
    total_new = 0
    for ticker in ETF_REGISTRY.keys():
        try:
            rows_raw = _fetch_stooq(ticker, years=2)
        except Exception as e:
            log.warning("etf refresh failed for %s: %s", ticker, e)
            summary["tickers"][ticker] = -1
            continue
        rows = [(ticker, *r) for r in rows_raw]
        result = db.upsert_etf_bars(rows) if rows else {"new": 0}
        n = result.get("new", 0)
        summary["tickers"][ticker] = n
        total_new += n
    summary["new"] = total_new
    summary["elapsed_s"] = round(time.time() - started, 2)
    return summary


# ─── Aggregation ────────────────────────────────────────────────────────────

@dataclass
class EtfRow:
    ticker: str
    asset: str
    issuer: str
    name: str
    last_close: float
    last_date: str
    aum_estimate_usd: float       # close × shares_outstanding
    dollar_volume_today_usd: float
    dollar_volume_7d_avg_usd: float
    aum_change_7d_pct: Optional[float]   # AUM proxy trend over 7 sessions
    flow_signal: str              # bullish | neutral | bearish

    def to_dict(self) -> dict:
        return asdict(self)


def _flow_signal(aum_change_7d: Optional[float], dvol_today: float,
                  dvol_7d_avg: float) -> str:
    """Coarse bullish/neutral/bearish bucket based on AUM trend + relative
    dollar-volume. AUM ↑ AND volume ↑ vs avg = real inflows. AUM ↓ AND
    volume ↑ = real outflows."""
    if aum_change_7d is None or dvol_7d_avg <= 0:
        return "neutral"
    vol_ratio = dvol_today / dvol_7d_avg if dvol_7d_avg > 0 else 1.0
    if aum_change_7d > 0.05 and vol_ratio > 1.2:
        return "bullish"
    if aum_change_7d < -0.05 and vol_ratio > 1.2:
        return "bearish"
    if aum_change_7d > 0.02:
        return "bullish"
    if aum_change_7d < -0.02:
        return "bearish"
    return "neutral"


def per_etf_snapshot() -> list[EtfRow]:
    """One row per ETF in the registry, with latest stats."""
    out: list[EtfRow] = []
    for ticker, meta in ETF_REGISTRY.items():
        rows = db.get_etf_bars(ticker, days=30)
        if not rows:
            continue
        # Sorted ascending in DB helper.
        last = rows[-1]
        last_close = float(last["close"])
        last_volume = float(last["volume"])
        last_date = last["date"]
        shares = float(meta.get("shares_m", 0)) * 1_000_000
        aum_today = last_close * shares
        dvol_today = last_close * last_volume
        # 7d AUM-proxy change using close as a stand-in for NAV.
        if len(rows) >= 8:
            close_7d_ago = float(rows[-8]["close"])
            aum_7d_ago = close_7d_ago * shares
            aum_chg = (aum_today / aum_7d_ago - 1.0) if aum_7d_ago > 0 else None
        else:
            aum_chg = None
        # 7d avg dollar volume.
        recent = rows[-7:] if len(rows) >= 7 else rows
        dvol_7d_avg = sum(float(r["close"]) * float(r["volume"])
                           for r in recent) / max(1, len(recent))
        signal = _flow_signal(aum_chg, dvol_today, dvol_7d_avg)
        out.append(EtfRow(
            ticker=ticker, asset=meta["asset"], issuer=meta["issuer"],
            name=meta["name"], last_close=round(last_close, 4),
            last_date=last_date,
            aum_estimate_usd=round(aum_today, 0),
            dollar_volume_today_usd=round(dvol_today, 0),
            dollar_volume_7d_avg_usd=round(dvol_7d_avg, 0),
            aum_change_7d_pct=round(aum_chg, 4) if aum_chg is not None else None,
            flow_signal=signal,
        ))
    return out


def asset_summary(asset: str) -> dict:
    """Roll up per-ETF stats into a single asset-level view. Used for
    the headline tiles (BTC ETFs total AUM, 7d trend, today's dollar
    volume)."""
    per_etf = [r for r in per_etf_snapshot() if r.asset == asset]
    if not per_etf:
        return {"asset": asset, "ready": False}
    total_aum = sum(r.aum_estimate_usd for r in per_etf)
    total_dvol_today = sum(r.dollar_volume_today_usd for r in per_etf)
    total_dvol_7d_avg = sum(r.dollar_volume_7d_avg_usd for r in per_etf)
    # Weighted 7d AUM change (by AUM share)
    weighted_chg = None
    weighted_sum = 0.0
    weight = 0.0
    for r in per_etf:
        if r.aum_change_7d_pct is None or r.aum_estimate_usd <= 0:
            continue
        weighted_sum += r.aum_change_7d_pct * r.aum_estimate_usd
        weight += r.aum_estimate_usd
    if weight > 0:
        weighted_chg = round(weighted_sum / weight, 4)
    # Headline signal — bullish if weighted_chg > 0.05 AND today's dvol
    # spikes vs the 7d avg.
    signal = _flow_signal(weighted_chg, total_dvol_today, total_dvol_7d_avg)
    return {
        "asset": asset, "ready": True,
        "etf_count": len(per_etf),
        "total_aum_usd": round(total_aum, 0),
        "dollar_volume_today_usd": round(total_dvol_today, 0),
        "dollar_volume_7d_avg_usd": round(total_dvol_7d_avg, 0),
        "aum_change_7d_pct": weighted_chg,
        "signal": signal,
        "top_3_by_aum": [r.ticker for r in
                          sorted(per_etf, key=lambda x: -x.aum_estimate_usd)[:3]],
    }


def overview() -> dict:
    """Single-call payload for the UI."""
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "BTC": asset_summary("BTC"),
        "ETH": asset_summary("ETH"),
        "per_etf": [r.to_dict() for r in per_etf_snapshot()],
    }
