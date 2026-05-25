"""Polymarket CLOB price history for a single market.

Best-effort URL: api.elections.kalshi… no wait, Polymarket CLOB lives at
clob.polymarket.com/prices-history. The endpoint takes a CLOB token ID
(one of the two outcome tokens; we pick the YES side) and returns a list
of {t, p} pairs — UNIX timestamps in seconds plus implied probability.

Used by /api/market-history?id=<conditionId> which is lazily called by the
frontend when a user expands a market row. If the endpoint is wrong or
the response shape has drifted, this returns None and the detail panel
just shows the static info without a sparkline.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from .. import cache, http

logger = logging.getLogger("climate.polymarket_history")

CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"


def _extract_yes_token_id(market: dict) -> Optional[str]:
    """Pull the YES outcome's CLOB token ID out of a gamma market record.

    Gamma's ``clobTokenIds`` is JSON-stringified by historical accident
    (e.g. ``'["yes_id", "no_id"]'``) so we json.loads it. The first
    element is the YES side by convention.
    """
    raw = market.get("clobTokenIds")
    if not raw:
        return None
    if isinstance(raw, list):
        ids = raw
    else:
        try:
            ids = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not ids or not isinstance(ids, list):
        return None
    yes = ids[0]
    return str(yes) if yes else None


def parse(payload) -> list[dict]:
    """Normalise the CLOB response into [{t, p}, ...].

    The CLOB documented response is ``{"history": [{"t": <unix>, "p": <0-1>}, …]}``.
    We accept either that or a bare list for resilience.
    """
    if isinstance(payload, dict):
        rows = payload.get("history") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            t = int(r["t"])
            p = float(r["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= p <= 1:
            out.append({"t": t, "p": round(p, 4)})
    return out


def fetch(market: dict, *, interval: str = "1d", fidelity: int = 60) -> Optional[list[dict]]:
    """Fetch + parse the CLOB price history for one market.

    Cached per token_id since this hits a per-market endpoint; with 100+
    climate markets we can't burn ~minutes of upstream load on every page
    refresh.
    """
    token = _extract_yes_token_id(market)
    if not token:
        return None
    cache_key = f"clob_history:{token}:{interval}:{fidelity}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    r = http.get(CLOB_HISTORY_URL,
                  params={"market": token, "interval": interval, "fidelity": fidelity},
                  timeout=10)
    if not r:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    series = parse(data)
    if not series:
        return None
    cache.set(cache_key, series)
    return series
