#!/usr/bin/env python3
"""
Macro overlay — DXY, US 10Y, gold, M2, VIX. These don't predict crypto
directly but they're the second-derivative drivers that turn cycles. The
single most reliable macro tell: DXY rallies kill crypto rallies.

Data sources:
  - FRED (free, optional API key from https://fred.stlouisfed.org)
      DTWEXBGS = broad trade-weighted USD index
      DGS10    = US 10-year Treasury yield
      M2SL     = M2 money stock (monthly, lagged)
      VIXCLS   = VIX (S&P 500 implied vol)
  - Stooq (free, no key) for gold (XAUUSD) and a DXY backup
  - All series stored in crypto_macro_series.

If FRED_API_KEY is not set, we fall back to Stooq for what we can get and
gracefully report 'unavailable' for the rest.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import requests

import database as db
import long_term as lt

log = logging.getLogger("crypto.macro")

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
STOOQ_BASE = "https://stooq.com/q/d/l/"

# (series_id, source, friendly_name, frequency_hint)
SERIES = [
    ("DXY",   "stooq",  "US Dollar Index",        "daily"),
    ("DGS10", "fred",   "US 10-Year Yield",       "daily"),
    ("VIXCLS","fred",   "VIX (S&P 500 IV)",       "daily"),
    ("M2SL",  "fred",   "M2 Money Stock",         "monthly"),
    ("XAUUSD","stooq",  "Gold (USD/oz)",          "daily"),
    # Stooq fallback if FRED key not set.
    ("DTWEXBGS","fred", "Broad USD (trade-wt)",   "daily"),
]


# ─── Fetchers ───────────────────────────────────────────────────────────────

def _safe_get(url: str, params: dict | None = None, timeout: int = 20):
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == 2:
                log.warning("macro fetch failed %s: %s", url, e)
                return None
            time.sleep(1 + attempt)
    return None


def fetch_fred(series_id: str, start: str) -> list[tuple[str, float]]:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        return []
    r = _safe_get(FRED_BASE, {
        "series_id": series_id, "api_key": key, "file_type": "json",
        "observation_start": start,
    })
    if not r:
        return []
    try:
        obs = r.json().get("observations", [])
    except ValueError:
        return []
    out = []
    for o in obs:
        try:
            if o.get("value") in (None, "", "."):
                continue
            out.append((o["date"], float(o["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_stooq(symbol: str, years: int = 4) -> list[tuple[str, float]]:
    """Stooq exports CSV with no auth. `symbol` is the Stooq code:
    DXY = ^dxy, XAUUSD = xauusd."""
    code_map = {"DXY": "^dxy", "XAUUSD": "xauusd"}
    code = code_map.get(symbol, symbol.lower())
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=365 * years)
    r = _safe_get(STOOQ_BASE, {
        "s": code, "d1": start.strftime("%Y%m%d"),
        "d2": end.strftime("%Y%m%d"), "i": "d",
    })
    if not r or not r.text:
        return []
    out = []
    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        try:
            d = row.get("Date") or row.get("date")
            c = row.get("Close") or row.get("close")
            if not d or not c:
                continue
            out.append((d, float(c)))
        except (TypeError, ValueError):
            continue
    return out


# ─── Refresh job ────────────────────────────────────────────────────────────

def refresh_series(series_id: str, source: str) -> int:
    """Pull and upsert one macro series. Returns rows inserted."""
    last = db.get_latest_macro_date(series_id)
    start = (datetime.fromisoformat(last) if last
             else datetime.now(timezone.utc) - timedelta(days=365 * 4)).date().isoformat()
    if source == "fred":
        rows_raw = fetch_fred(series_id, start)
    elif source == "stooq":
        rows_raw = fetch_stooq(series_id, years=4)
    else:
        return 0
    rows = [(series_id, d, v) for d, v in rows_raw if d > (last or "")]
    if rows:
        db.upsert_macro_series(rows)
    return len(rows)


def refresh_all_macro() -> dict:
    started = time.time()
    out = {"series": {}, "elapsed_s": 0.0}
    seen = set()
    for series_id, source, _, _ in SERIES:
        if series_id in seen:
            continue
        seen.add(series_id)
        try:
            out["series"][series_id] = refresh_series(series_id, source)
        except Exception as e:
            log.warning("macro refresh failed for %s: %s", series_id, e)
            out["series"][series_id] = -1
    out["elapsed_s"] = round(time.time() - started, 2)
    return out


# ─── Analytics ──────────────────────────────────────────────────────────────

@dataclass
class MacroSnapshot:
    series_id: str
    name: str
    value: Optional[float]
    pct_change_30d: Optional[float]
    pct_change_365d: Optional[float]
    btc_corr_90d: Optional[float]      # Pearson corr with BTC daily returns
    signal: str                         # crypto-tailwind | crypto-headwind | neutral
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


def _series_to_array(rows: list[dict]) -> tuple[list[str], np.ndarray]:
    dates = [r["date"] for r in rows]
    vals = np.asarray([r["value"] for r in rows], dtype=np.float64)
    return dates, vals


def _aligned_returns(macro_rows: list[dict], btc_dates: list[str], btc_closes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Forward-fill the macro series to BTC's daily grid and return paired log-returns."""
    macro_by_date = {r["date"]: r["value"] for r in macro_rows}
    aligned = []
    last = None
    for d in btc_dates:
        v = macro_by_date.get(d, last)
        aligned.append(v)
        last = v if v is not None else last
    arr = np.asarray([np.nan if v is None else v for v in aligned], dtype=np.float64)
    valid = np.isfinite(arr) & np.isfinite(btc_closes)
    if valid.sum() < 30:
        return np.array([]), np.array([])
    a = arr[valid]
    b = btc_closes[valid]
    ra = np.diff(np.log(a)) if np.all(a > 0) else np.diff(a)
    rb = np.diff(np.log(b))
    n = min(len(ra), len(rb))
    return ra[-n:], rb[-n:]


def macro_snapshot(series_id: str, friendly: str) -> MacroSnapshot:
    rows = db.get_macro_series(series_id, days=400)
    if not rows:
        return MacroSnapshot(series_id, friendly, None, None, None, None,
                             "unavailable", "no data")
    _, vals = _series_to_array(rows)
    cur = float(vals[-1])
    c30 = float(vals[-1] / vals[-30] - 1.0) if len(vals) >= 30 and vals[-30] != 0 else None
    c365 = float(vals[-1] / vals[-365] - 1.0) if len(vals) >= 365 and vals[-365] != 0 else None

    # BTC correlation over the last 90 days.
    btc_dates, btc_closes = lt.get_daily_closes("BTC", days=120)
    corr = None
    if len(btc_closes) >= 30:
        ra, rb = _aligned_returns(rows[-120:], btc_dates, btc_closes)
        if len(ra) >= 30:
            corr = float(np.corrcoef(ra[-90:], rb[-90:])[0, 1])

    # Crypto tailwind/headwind heuristic — series-specific.
    signal = "neutral"
    desc = f"{friendly} at {cur:.2f}"
    if series_id in ("DXY", "DTWEXBGS"):
        if c30 is not None and c30 > 0.02:
            signal, desc = "crypto-headwind", f"USD up {c30*100:+.1f}% / 30d — crypto headwind"
        elif c30 is not None and c30 < -0.02:
            signal, desc = "crypto-tailwind", f"USD down {c30*100:+.1f}% / 30d — crypto tailwind"
    elif series_id == "DGS10":
        if cur > 4.5:
            signal, desc = "crypto-headwind", f"10Y at {cur:.2f}% — restrictive"
        elif cur < 2.5:
            signal, desc = "crypto-tailwind", f"10Y at {cur:.2f}% — easy"
    elif series_id == "VIXCLS":
        if cur > 25:
            signal, desc = "crypto-headwind", f"VIX {cur:.1f} — risk-off regime"
        elif cur < 14:
            signal, desc = "crypto-tailwind", f"VIX {cur:.1f} — risk-on regime"
    elif series_id == "M2SL":
        if c365 is not None and c365 > 0.05:
            signal, desc = "crypto-tailwind", f"M2 up {c365*100:+.1f}% / 1y — liquidity expanding"
        elif c365 is not None and c365 < 0:
            signal, desc = "crypto-headwind", f"M2 down {c365*100:+.1f}% / 1y — liquidity tightening"
    elif series_id == "XAUUSD":
        if c30 is not None and c30 > 0.05:
            signal, desc = "neutral", f"Gold up {c30*100:+.1f}% / 30d — alternative store-of-value bid"

    return MacroSnapshot(
        series_id=series_id, name=friendly, value=round(cur, 4),
        pct_change_30d=round(c30, 4) if c30 is not None else None,
        pct_change_365d=round(c365, 4) if c365 is not None else None,
        btc_corr_90d=round(corr, 3) if corr is not None else None,
        signal=signal, description=desc,
    )


def macro_overview() -> list[dict]:
    """One snapshot per series, in the canonical display order."""
    return [macro_snapshot(sid, friendly).to_dict()
            for sid, _, friendly, _ in SERIES]


def macro_regime() -> dict:
    """Single composite macro regime: -1 (risk-off / crypto-headwind) ... +1 (tailwind)."""
    snaps = macro_overview()
    scores = []
    for s in snaps:
        if s["signal"] == "crypto-tailwind":
            scores.append(1)
        elif s["signal"] == "crypto-headwind":
            scores.append(-1)
        elif s["signal"] == "neutral":
            scores.append(0)
    if not scores:
        return {"score": None, "label": "no-data", "components": snaps}
    avg = float(np.mean(scores))
    if avg > 0.4:
        label = "tailwind"
    elif avg > 0.1:
        label = "lean-tailwind"
    elif avg > -0.1:
        label = "neutral"
    elif avg > -0.4:
        label = "lean-headwind"
    else:
        label = "headwind"
    return {"score": round(avg, 3), "label": label, "components": snaps}
