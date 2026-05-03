"""Full Fed Funds futures strip — the OIS curve out 12-24 months.

Why this is different from :mod:`implied_path`:

  * ``implied_path`` answers *"what's the implied move at the **next** FOMC?"*
    by reading a single contract — the one for the month immediately after
    the meeting (clean, no intra-month weighting).
  * ``ois_curve`` answers *"where does the market expect the Fed Funds rate
    to be each month for the next year?"* by reading the **whole contract
    strip** and reporting each month's implied average rate.

For each month ``M`` in the next ``months_ahead`` months:

      contract_price_M = 100 − avg_FF_rate_during_M

So ``implied_avg_rate_M = 100 − contract_price_M`` is the market's expected
**monthly average** Fed Funds rate during month ``M``. Months that contain
an FOMC will straddle two rate regimes and the average sits between them;
months without an FOMC reflect the single prevailing post-decision rate.

The result is a step-like rate path that visualizes Polymarket "Fed pivot in
Q3" and "first cut in March 2027" markets, not just the next-meeting bet.

Source: Yahoo Finance ZQ contract chain. Free, no key, same auth-less
endpoint :mod:`implied_path` already uses. Cache 30 min — same TTL.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from threading import Lock

from . import implied_path

log = logging.getLogger(__name__)


_CACHE: dict = {"data": None, "fetched_at": 0.0, "key": None}
_CACHE_TTL = 30 * 60  # 30 min — same as next-FOMC implied
_lock = Lock()


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    """Return (year, month) ``n`` months after ``(year, month)``."""
    idx = (year * 12 + (month - 1)) + n
    return idx // 12, (idx % 12) + 1


def fetch_curve(months_ahead: int = 18, start: date | None = None) -> list[dict]:
    """Build the implied-rate path month-by-month for the next ``months_ahead``
    months. Each entry has the contract symbol used, the contract price, the
    implied average rate, and a YYYY-MM stamp for charting."""
    today = start or datetime.now(timezone.utc).date()
    out: list[dict] = []
    for i in range(months_ahead):
        y, m = _add_months(today.year, today.month, i)
        symbol = implied_path.ff_contract_symbol(y, m)
        price = implied_path._fetch_yahoo_close(symbol)
        if price is None:
            # Don't break — try the next month, since some contracts (e.g.
            # the very-front month near settlement) sometimes return null.
            log.info("OIS curve: %s missing, skipping", symbol)
            continue
        implied = 100.0 - price
        out.append({
            "year": y,
            "month": m,
            "ym": f"{y:04d}-{m:02d}",
            "contract_symbol": symbol,
            "contract_price": round(price, 4),
            "implied_avg_rate": round(implied, 4),
            "months_out": i,
        })
    return out


def get_cached(months_ahead: int = 18, force: bool = False) -> dict:
    now = time.time()
    key = months_ahead
    with _lock:
        fresh = (
            _CACHE["data"] is not None
            and _CACHE["key"] == key
            and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        )
        if fresh and not force:
            return _CACHE["data"]

    curve = fetch_curve(months_ahead=months_ahead)
    today = datetime.now(timezone.utc).date()

    # Anchor the curve to the current spot rate from FRED's DFF — gives the
    # frontend a single source of truth for "where rates are today".
    spot = None
    try:
        from . import fred_client
        rates = fred_client.get_cached_rates()
        dff = next((s for s in rates["series"] if s["series_id"] == "DFF"), None)
        if dff and dff.get("latest"):
            spot = float(dff["latest"][1])
    except Exception as exc:
        log.warning("OIS curve spot-rate lookup failed: %s", exc)

    data = {
        "as_of": today.isoformat(),
        "spot_rate": spot,
        "curve": curve,
        "method": "monthly ZQ contract → implied avg FF rate per month",
    }
    with _lock:
        _CACHE["data"] = data
        _CACHE["fetched_at"] = now
        _CACHE["key"] = key
    return data


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(get_cached(force=True), indent=2))
