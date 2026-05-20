"""Polymarket Gamma API client — regulator-action markets.

Fetches active markets from `https://gamma-api.polymarket.com/markets`
and keeps the ones whose question mentions at least one anchor token
(regulator code, ETF, stablecoin, named exchange, etc. — see
`analysis/market_match.ANCHOR_TOKENS`). The full universe of Polymarket
markets is too large to ship to the client per request, so we pre-filter
server-side to the ~50-200 likely-relevant ones.

Cache: 5 min — market prices move on the minute but RSS feeds don't.
Tighter polling burns API quota without surfacing a different signal.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from threading import Lock

log = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
UA = "regulators-dashboard/0.5"

_CACHE_TTL = 5 * 60
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_lock = Lock()


def _fetch_page(params: dict, timeout: float = 20.0) -> list[dict]:
    url = f"{GAMMA_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read(10_000_000)
    body = json.loads(raw.decode("utf-8", errors="replace"))
    # Gamma returns a list directly, not wrapped.
    return body if isinstance(body, list) else body.get("markets", [])


def _parse_outcome_prices(raw) -> tuple[float | None, float | None]:
    """outcome_prices may arrive as a JSON-encoded string `'["0.14","0.86"]'`
    or as a list. Return (yes_price, no_price) as floats in [0, 1] or (None, None).
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(raw, list) or len(raw) < 2:
        return None, None
    try:
        return float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None, None


def _normalize(market: dict) -> dict | None:
    """Project a Gamma market to our shared shape. Returns None if it's not
    a binary YES/NO market we can use."""
    question = (market.get("question") or "").strip()
    if not question:
        return None
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = None
    if not isinstance(outcomes, list) or len(outcomes) < 2:
        return None
    # Heuristic: keep only Yes/No binary markets — non-binary categorical
    # markets need a different UI affordance and are deferred.
    labels = [str(o).strip().lower() for o in outcomes]
    if set(labels[:2]) != {"yes", "no"}:
        return None

    yes_price, no_price = _parse_outcome_prices(market.get("outcomePrices") or market.get("outcome_prices"))

    slug = market.get("slug") or ""
    event_slug = market.get("eventSlug") or market.get("event_slug") or ""
    url = (
        f"https://polymarket.com/event/{event_slug}"
        if event_slug
        else (f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com")
    )
    return {
        "source": "polymarket",
        "id": str(market.get("id") or market.get("conditionId") or slug),
        "question": question,
        "yes_price": yes_price,
        "no_price": no_price,
        "end_date": market.get("endDate") or market.get("end_date_iso") or market.get("endDateIso"),
        "url": url,
        "volume": market.get("volume") or market.get("volume24h"),
    }


def fetch_all(limit: int = 500) -> list[dict]:
    """Pull active, open markets and normalize. Single page — Gamma's
    default limit is generous and we don't need historical depth."""
    raw = _fetch_page({"active": "true", "closed": "false", "limit": str(limit)})
    out: list[dict] = []
    for m in raw:
        norm = _normalize(m)
        if norm:
            out.append(norm)
    return out


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = _CACHE["data"] is not None and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        if fresh and not force:
            return _CACHE["data"]
    try:
        markets = fetch_all()
        payload = {"ok": True, "markets": markets, "count": len(markets), "error": None}
    except Exception as exc:
        log.warning("Polymarket fetch failed: %s", exc)
        payload = {"ok": False, "markets": [], "count": 0, "error": str(exc)}
    payload["fetched_at"] = now
    with _lock:
        _CACHE["data"] = payload
        _CACHE["fetched_at"] = now
    return payload


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    data = get_cached(force=True)
    print(f"ok={data['ok']}  count={data['count']}  err={data['error']}")
    for m in data["markets"][:3]:
        print(f"  yes={m['yes_price']!s:<6}  {m['question'][:90]}")
    if not data["ok"]:
        sys.exit(0)
