"""Polymarket historical-price fetcher.

The dashboard's `weather_price_snapshots` only goes back as far as the
snapshot loop has been running. For a credible backtest we want the full
price path for every closed weather market, which Polymarket exposes via
its CLOB prices-history endpoint:

    GET https://clob.polymarket.com/prices-history?market=<token_id>&interval=1d&fidelity=60

Two important details:

  * `market` here is the **token id** of one outcome (YES or NO), not the
    Gamma `id` of the market wrapper. We accept either an explicit
    `clobTokenIds[0]` or fall back to fetching the CLOB market by
    `condition_id` to discover it.
  * Polymarket caps history per request, so for older markets we paginate
    by walking back the `startTs`. The endpoint also accepts `interval`
    aliases (1m, 1w, 1d, 6h, 1h) and a `fidelity` in minutes.

Nothing here writes to the DB — callers (backtest, manual scripts) decide
where to put the data.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

import requests

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


def _safe_get(url: str, params: Optional[dict] = None, timeout: int = 15,
              retries: int = 3, backoff: float = 1.5) -> Optional[dict]:
    """GET with exponential backoff on 429/5xx. Returns parsed JSON or None."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "narve-weather-backtest/1.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = backoff ** (attempt + 1)
                logger.info("Polymarket 429, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            if 500 <= r.status_code < 600:
                wait = backoff ** (attempt + 1)
                time.sleep(wait)
                continue
            logger.debug("Polymarket %d for %s", r.status_code, url)
            return None
        except requests.RequestException as e:
            logger.debug("Polymarket request failed: %s", e)
            time.sleep(backoff ** (attempt + 1))
    return None


def find_yes_token_id(market_id: str, condition_id: Optional[str] = None,
                      clob_token_ids: Optional[list] = None) -> Optional[str]:
    """Resolve a Polymarket market_id (Gamma id) into the YES outcome's CLOB
    token id. We accept the precomputed token ids if the caller already has
    them — saves a network round-trip per market."""
    if clob_token_ids and len(clob_token_ids) >= 1 and clob_token_ids[0]:
        return str(clob_token_ids[0])
    cond = condition_id or market_id
    if not cond:
        return None
    data = _safe_get(f"{CLOB_BASE}/markets/{cond}")
    if not data:
        return None
    tokens = data.get("tokens") or []
    for t in tokens:
        if str(t.get("outcome", "")).lower() == "yes":
            return str(t.get("token_id"))
    if tokens:
        return str(tokens[0].get("token_id"))
    return None


def fetch_price_history(token_id: str, start_ts: Optional[int] = None,
                        end_ts: Optional[int] = None,
                        interval: str = "1h",
                        fidelity_minutes: int = 60) -> list[dict]:
    """Pull a price history series for a CLOB token id.

    Returns a list of ``{t: unix_seconds, p: yes_price}``. An empty list
    means "no data" — callers should not treat that as an error since
    very fresh or extremely thin markets legitimately have no history.
    """
    if not token_id:
        return []
    params: dict = {"market": token_id, "interval": interval,
                    "fidelity": int(fidelity_minutes)}
    if start_ts is not None:
        params["startTs"] = int(start_ts)
    if end_ts is not None:
        params["endTs"] = int(end_ts)
    data = _safe_get(f"{CLOB_BASE}/prices-history", params=params)
    if not data:
        return []
    history = data.get("history") or []
    out = []
    for row in history:
        t = row.get("t") or row.get("timestamp")
        p = row.get("p") or row.get("price")
        if t is None or p is None:
            continue
        try:
            out.append({"t": int(t), "p": float(p)})
        except (TypeError, ValueError):
            continue
    return out


def fetch_price_at_lead(token_id: str, target_unix: int,
                        lead_seconds: int) -> Optional[float]:
    """Get the YES price ``lead_seconds`` before ``target_unix``.

    Uses a short window around the desired moment so the backtest can
    answer "what would I have paid at T-7 days?" without importing the
    full price path. Returns the price at the closest available timestamp,
    or None.
    """
    if not token_id or target_unix is None:
        return None
    pivot = int(target_unix - lead_seconds)
    window = 6 * 3600  # 6h tolerance
    rows = fetch_price_history(token_id, start_ts=pivot - window,
                               end_ts=pivot + window, interval="1h",
                               fidelity_minutes=60)
    if not rows:
        return None
    rows.sort(key=lambda r: abs(r["t"] - pivot))
    return rows[0]["p"]


def fetch_closed_weather_markets(tag_slug: str = "weather",
                                 limit: int = 500) -> list[dict]:
    """Walk Polymarket's Gamma API for closed weather markets. Used by the
    backtest to enumerate everything resolvable.

    Returns a list of dicts with ``id``, ``question``, ``conditionId``,
    ``clobTokenIds``, ``closedTime``, ``endDate``, etc.
    """
    out: list[dict] = []
    offset = 0
    while True:
        params = {
            "tag_slug": tag_slug,
            "closed": "true",
            "limit": min(100, limit - len(out)),
            "offset": offset,
            "order": "endDate",
            "ascending": "false",
        }
        data = _safe_get(f"{GAMMA_BASE}/events", params=params)
        if not data:
            break
        if isinstance(data, dict):
            events = data.get("events") or data.get("data") or []
        else:
            events = data
        if not events:
            break
        for ev in events:
            for m in ev.get("markets") or [ev]:
                if not isinstance(m, dict):
                    continue
                out.append(m)
        offset += len(events)
        if len(out) >= limit or len(events) < params["limit"]:
            break
    return out


def stitch_walkforward_prices(token_id: str, target_unix: int,
                              leads_days: Iterable[int] = (1, 3, 7, 14),
                              ) -> dict:
    """Convenience wrapper: fetch the YES price at each requested lead time
    in days. Returns ``{lead_days: yes_price}`` with missing leads omitted."""
    out: dict = {}
    for d in leads_days:
        p = fetch_price_at_lead(token_id, target_unix, d * 86400)
        if p is not None:
            out[d] = p
    return out
