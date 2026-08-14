"""Smart-money signal for midterm races.

Pulls the ``/api/smart-money`` flow data from the ``top-traders-dashboard``
service (which scans top-quality Polymarket wallets for consensus positions)
and joins each flow to a midterm race by matching on the Polymarket event
slug stored on the source row.

Per-race output::

    {
      "race_key": "senate_TX",
      "available": True,
      "total_smart_usd": 124_300.0,
      "smart_wallet_count": 11,
      "avg_quality": 78.4,
      "direction": "D" | "R" | None,
      "lean_strength": 0.0..1.0,            # fraction of $ on the leading party
      "flows": [ {market_id, outcome, total_position_usd, ...}, ... ],
    }

Cached via Redis (``smart_money:flows``) for SMART_MONEY_TTL seconds because
the upstream scanner only re-runs every 30 minutes; pummeling its endpoint
on every page load would be silly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from typing import Optional

import aiohttp

from cache import cache

logger = logging.getLogger(__name__)

# Where to reach top-traders-dashboard. In docker-compose the service is
# resolvable by name; for local dev override via env var.
TOP_TRADERS_URL = os.environ.get("TOP_TRADERS_URL", "http://top-traders:8052")
SMART_MONEY_TTL = int(os.environ.get("SMART_MONEY_TTL", "300"))  # seconds
HTTP_TIMEOUT = 15.0

# Top-traders requires the gateway HMAC header on every call.
_GATEWAY_SECRET_ENV = "GATEWAY_SSO_SECRET"


# Reuse the same Yes/No-aware party classifier as the divergence overview so
# "Yes" on "Will Democrats win Texas?" maps to D.
_DEM_NAME_RE = re.compile(r"\b(democrat|dems?|democratic|d\.?)\b", re.I)
_REP_NAME_RE = re.compile(r"\b(republican|reps?|gop|r\.?)\b", re.I)


def _classify_outcome_party(outcome_name: str, market_title: str) -> Optional[str]:
    name = (outcome_name or "").strip().lower()
    title = (market_title or "").lower()
    if _DEM_NAME_RE.search(name):
        return "democrat"
    if _REP_NAME_RE.search(name):
        return "republican"
    if name in {"yes", "no"}:
        if "democrat" in title:
            return "democrat" if name == "yes" else "republican"
        if "republican" in title or "gop" in title:
            return "republican" if name == "yes" else "democrat"
    return None


async def fetch_smart_money_flows(session: aiohttp.ClientSession) -> dict:
    """Fetch the global smart-money flow list, with Redis caching.

    Returns ``{"flows": [...], "available": bool}`` so callers can distinguish
    a cold start (no data yet) from an upstream outage.
    """
    cache_key = "midterm:smart_money:flows"

    if cache.available:
        try:
            raw = cache._r.get(cache_key)  # type: ignore[attr-defined]
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"smart-money cache get error: {e}")

    secret = os.environ.get(_GATEWAY_SECRET_ENV, "")
    if not secret:
        logger.info("smart-money: GATEWAY_SSO_SECRET not set; skipping fetch")
        return {"flows": [], "available": False, "reason": "no_secret"}

    url = f"{TOP_TRADERS_URL.rstrip('/')}/api/smart-money"
    headers = {"x-gateway-secret": secret}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as resp:
            if resp.status != 200:
                logger.info(f"smart-money upstream returned {resp.status}")
                return {"flows": [], "available": False, "reason": f"http_{resp.status}"}
            data = await resp.json()
    except asyncio.TimeoutError:
        logger.warning("smart-money fetch timed out")
        return {"flows": [], "available": False, "reason": "timeout"}
    except Exception as e:
        logger.warning(f"smart-money fetch error: {e}")
        return {"flows": [], "available": False, "reason": "error"}

    flows = data.get("flows", []) if isinstance(data, dict) else []
    payload = {"flows": flows, "available": True}

    if cache.available:
        try:
            cache._r.setex(cache_key, SMART_MONEY_TTL, json.dumps(payload))  # type: ignore[attr-defined]
        except Exception as e:
            logger.debug(f"smart-money cache set error: {e}")

    return payload


def _flow_index_by_slug(flows: list[dict]) -> dict[str, list[dict]]:
    """Index flows by event/market slug for O(1) per-race lookup."""
    out: dict[str, list[dict]] = defaultdict(list)
    for f in flows:
        slug = (f.get("slug") or "").strip().lower()
        if slug:
            out[slug].append(f)
    return out


def race_smart_money(
    *,
    race_key: str,
    race_polymarket_markets: list[dict],
    flows: list[dict],
) -> dict:
    """Aggregate smart-money flows for a single midterm race.

    Args:
      race_key: The canonical race key (echoed back in the response).
      race_polymarket_markets: Rows from ``midterm_markets`` with
        ``source == 'polymarket'`` for this race. We use their ``slug`` to
        join against the smart-money flow list.
      flows: The flow list from ``fetch_smart_money_flows``.

    Returns the schema documented at the top of this module.
    """
    if not race_polymarket_markets or not flows:
        return {
            "race_key": race_key,
            "available": False,
            "total_smart_usd": 0.0,
            "smart_wallet_count": 0,
            "avg_quality": 0.0,
            "direction": None,
            "lean_strength": 0.0,
            "flows": [],
        }

    index = _flow_index_by_slug(flows)

    matched_flows: list[dict] = []
    by_party: dict[str, float] = {"democrat": 0.0, "republican": 0.0}
    distinct_wallets: set[str] = set()
    quality_sum = 0.0
    quality_n = 0

    for market in race_polymarket_markets:
        slug = (market.get("slug") or "").strip().lower()
        if not slug:
            continue
        flow_list = index.get(slug, [])
        for flow in flow_list:
            outcome = flow.get("outcome") or ""
            title = market.get("title") or market.get("event_title") or ""
            party = _classify_outcome_party(outcome, title)
            usd = float(flow.get("total_position_usd") or 0.0)
            if party:
                by_party[party] += usd
            for w in flow.get("wallets") or []:
                addr = (w.get("address") or "").lower()
                if addr:
                    distinct_wallets.add(addr)
            aq = flow.get("avg_quality")
            if aq is not None:
                try:
                    quality_sum += float(aq) * (flow.get("smart_wallet_count") or 1)
                    quality_n += int(flow.get("smart_wallet_count") or 1)
                except (TypeError, ValueError):
                    pass
            matched_flows.append({
                "market_id": flow.get("market_id"),
                "outcome": outcome,
                "party": party,
                "total_position_usd": round(usd, 2),
                "smart_wallet_count": flow.get("smart_wallet_count", 0),
                "avg_quality": flow.get("avg_quality"),
                "wallets": flow.get("wallets", [])[:5],  # cap for payload size
            })

    total_smart_usd = by_party["democrat"] + by_party["republican"]
    if total_smart_usd <= 0:
        direction = None
        lean_strength = 0.0
    else:
        if by_party["democrat"] >= by_party["republican"]:
            direction = "D"
            lean_strength = by_party["democrat"] / total_smart_usd
        else:
            direction = "R"
            lean_strength = by_party["republican"] / total_smart_usd

    return {
        "race_key": race_key,
        "available": bool(matched_flows),
        "total_smart_usd": round(total_smart_usd, 2),
        "smart_wallet_count": len(distinct_wallets),
        "avg_quality": round(quality_sum / quality_n, 1) if quality_n else 0.0,
        "direction": direction,
        "lean_strength": round(lean_strength, 4),
        "by_party": {k: round(v, 2) for k, v in by_party.items()},
        "flows": matched_flows,
    }
