"""Market-implied next-FOMC rate from Fed Funds futures (ZQ contracts).

Method (standard CME):

    contract_price = 100 - average_FF_rate_during_contract_month

For a clean read on the *post-decision* rate, use the contract whose month
**immediately follows** the FOMC meeting. That contract's month contains no
FOMC decision, so its average is just the prevailing post-decision rate:

    implied_post_rate = 100 - price

Then bucket the implied move into discrete probabilities with linear
interpolation across 25-bp steps. This is the same heuristic CME's FedWatch
uses publicly — for trading, validate against CME's own numbers.

Source: Yahoo Finance chart API. Free, no key. Graceful-degrades on failure.
Cache: 30 min (futures move continuously; FRED DFF moves daily, so the
binding constraint here is the futures price).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import date, datetime, timezone
from threading import Lock

from . import decision_calendar, fred_client

log = logging.getLogger(__name__)

# CME / Globex month codes
MONTH_CODES = "FGHJKMNQUVXZ"  # Jan=F, Feb=G, ..., Dec=Z

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
# Yahoo blocks default urllib UA; use a realistic browser string.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

_CACHE: dict = {"data": None, "fetched_at": 0.0}
_CACHE_TTL = 30 * 60  # 30 min
_lock = Lock()


def ff_contract_symbol(year: int, month: int) -> str:
    """ZQ contract symbol on Yahoo, e.g. (2026, 5) -> 'ZQK26.CBT'."""
    return f"ZQ{MONTH_CODES[month - 1]}{year % 100:02d}.CBT"


def _next_month(d: date) -> tuple[int, int]:
    return (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)


def _fetch_yahoo_close(symbol: str, timeout: float = 10.0) -> float | None:
    """Most recent close price for `symbol`, or None on any failure."""
    url = YAHOO_CHART.format(symbol=symbol)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("Yahoo fetch failed for %s: %s", symbol, exc)
        return None

    try:
        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        # Latest non-null close
        for v in reversed(closes):
            if v is not None:
                return float(v)
    except (KeyError, IndexError, TypeError) as exc:
        log.warning("Yahoo payload malformed for %s: %s", symbol, exc)
    return None


def _derive_probs(delta_pct: float, step_pct: float = 0.25) -> dict:
    """Linear-interpolation buckets across 25-bp steps.

    Returns three ordered buckets centered on the integer-step move closest
    to `delta`. Examples (step = 0.25):
        delta = 0      -> {hold: 1.0, cut25: 0, hike25: 0}
        delta = -0.10  -> {hold: 0.6, cut25: 0.4, ...}
        delta = -0.30  -> {cut25: 0.8, cut50: 0.2, ...}
    """
    # Snap to the lower step boundary; e.g. -0.10 -> floor = 0 (hold), upper = -0.25 (cut25)
    # We linearly distribute mass between two adjacent step buckets.
    steps_below = int(delta_pct // step_pct) if delta_pct >= 0 else -int((-delta_pct) // step_pct)
    # Lower & upper bracketing steps (in units of step_pct)
    lower_n = steps_below
    upper_n = steps_below + (1 if delta_pct >= 0 else -1)
    # Fraction toward upper
    frac = abs((delta_pct - lower_n * step_pct) / step_pct)
    frac = max(0.0, min(1.0, frac))

    def label(n: int) -> str:
        if n == 0:
            return "hold"
        bps = abs(n) * int(step_pct * 100)
        return f"{'hike' if n > 0 else 'cut'}{bps}"

    if abs(delta_pct - lower_n * step_pct) < 1e-9:
        # Exactly on a step boundary
        return {label(lower_n): 1.0}
    return {label(lower_n): round(1 - frac, 3), label(upper_n): round(frac, 3)}


def compute() -> dict:
    """Compute next-FOMC implied move. Always returns a dict; populates
    `error` instead of raising when something is missing."""
    today = datetime.now(timezone.utc).date()
    cal = decision_calendar.upcoming(today, horizon_days=120)
    fomc = next((m for m in cal if m["cb"] == "US"), None)
    if not fomc:
        return {"error": "no upcoming FOMC in horizon", "as_of": today.isoformat()}

    meeting_date = date.fromisoformat(fomc["decision_date"])
    contract_yr, contract_mo = _next_month(meeting_date)
    symbol = ff_contract_symbol(contract_yr, contract_mo)
    price = _fetch_yahoo_close(symbol)

    # Current Fed Funds rate from cached FRED DFF.
    rates = fred_client.get_cached_rates()
    dff = next((s for s in rates["series"] if s["series_id"] == "DFF"), None)
    current_rate = dff["latest"][1] if dff and dff["latest"] else None

    if price is None or current_rate is None:
        return {
            "as_of": today.isoformat(),
            "meeting": fomc,
            "contract_symbol": symbol,
            "contract_price": price,
            "current_rate": current_rate,
            "error": "missing futures price or current rate",
        }

    implied_rate = 100.0 - price
    delta = implied_rate - current_rate
    probs = _derive_probs(delta)

    return {
        "as_of": today.isoformat(),
        "meeting": fomc,
        "contract_symbol": symbol,
        "contract_price": round(price, 4),
        "current_rate": current_rate,
        "implied_post_rate": round(implied_rate, 4),
        "delta_bps": round(delta * 100, 1),
        "probabilities": probs,
        "method": "ZQ next-month contract; CME FedWatch-style linear interpolation",
    }


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        if not force and _CACHE["data"] and (now - _CACHE["fetched_at"]) < _CACHE_TTL:
            return _CACHE["data"]
    data = compute()
    with _lock:
        _CACHE["data"] = data
        _CACHE["fetched_at"] = now
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(get_cached(force=True), indent=2))
