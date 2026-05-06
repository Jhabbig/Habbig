"""Map Polymarket disaster questions to a model probability + edge.

Each market is a dict from the gamma API plus our injected ``_event_title``
field. We pick the best-matching model based on keywords in the title,
extract the threshold from the question text, evaluate the model, and
attach ``_model_p``, ``_implied_p``, ``_edge_pp``, ``_rationale`` to the
market dict.

Models supported:

  1. Atlantic named-storm count: "Will there be at least N named storms?"
  2. M{x}+ earthquake count by year-end: "Will there be N+ M{x} quakes?"
  3. Wildfire-event count by year-end (EONET): "Will there be N+ wildfires?"

Where the threshold can't be parsed or no model fits, the market is still
returned but with ``_model_p`` = None.
"""
from __future__ import annotations

import re
from typing import Optional

from .poisson import p_at_least

_RE_AT_LEAST = re.compile(
    r"(?:at\s+least|more\s+than|over|exceed[a-z]*|>=?\s*|\bat\s+or\s+above)\s*(\d{1,4})", re.I)
_RE_FEWER = re.compile(
    r"(?:fewer\s+than|less\s+than|under|<=?\s*|below|no\s+more\s+than)\s*(\d{1,4})", re.I)
_RE_MAGNITUDE = re.compile(r"(?:m\s*|magnitude\s*)(\d(?:\.\d)?)", re.I)


def _parse_implied(market: dict) -> Optional[float]:
    for key in ("lastTradePrice", "bestBid", "bestAsk"):
        v = market.get(key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0.0 <= f <= 1.0:
            return f
    return None


def _is_atlantic_storm_market(tl: str) -> bool:
    return any(k in tl for k in ("named storm", "atlantic", "hurricane"))


def _is_wildfire_market(tl: str) -> bool:
    return any(k in tl for k in ("wildfire", "wild fire", "forest fire", "bushfire"))


def _is_quake_market(tl: str) -> bool:
    return any(k in tl for k in ("earthquake", "magnitude", "richter"))


def _quake_threshold_from_title(tl: str) -> Optional[float]:
    m = _RE_MAGNITUDE.search(tl)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _at_least_count(text: str) -> Optional[int]:
    m = _RE_AT_LEAST.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _at_most_count(text: str) -> Optional[int]:
    m = _RE_FEWER.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _score_storm_market(title: str, proj: dict) -> tuple[Optional[float], str]:
    """Atlantic-named-storm count market scoring.

    proj["lambda_remaining"] is the climo-prior Poisson rate for the rest of
    the season; proj["active_named_storms_ytd_lower_bound"] is the YTD count
    we already know about. We turn "at least N" into k = N - ytd remaining.
    """
    if not proj or proj.get("error"):
        return None, ""
    ytd = proj.get("active_named_storms_ytd_lower_bound") or 0
    lam = proj.get("lambda_remaining")
    if lam is None:
        return None, ""
    n = _at_least_count(title)
    if n is None:
        return None, ""
    needed = n - ytd
    p = p_at_least(lam, needed)
    if p is None:
        return None, ""
    rationale = (f"YTD {ytd} active + Poisson(lambda={lam}) for "
                 f"{proj.get('days_remaining_season')}d remaining; need {needed} more")
    return p, rationale


def _score_quake_market(title: str, projections_by_mag: dict) -> tuple[Optional[float], str]:
    """M{x}+ earthquake count market scoring."""
    mag = _quake_threshold_from_title(title)
    if mag is None:
        return None, ""
    # Round to the nearest integer projection we have (5, 6, 7).
    available = sorted(projections_by_mag.keys())
    nearest = min(available, key=lambda m: abs(m - mag))
    proj = projections_by_mag.get(nearest)
    if not proj or proj.get("error"):
        return None, ""
    n = _at_least_count(title)
    if n is None:
        return None, ""
    ytd = proj.get("ytd_count") or 0
    lam = proj.get("lambda_remaining")
    if lam is None:
        return None, ""
    needed = n - ytd
    p = p_at_least(lam, needed)
    if p is None:
        return None, ""
    rationale = (f"M{nearest}+ YTD {ytd} + Poisson(lambda={lam}) "
                 f"for {proj.get('days_remaining')}d remaining; need {needed} more")
    return p, rationale


def _score_wildfire_market(title: str, proj: dict) -> tuple[Optional[float], str]:
    """EONET-wildfire-event-count market scoring."""
    if not proj or proj.get("error"):
        return None, ""
    n = _at_least_count(title)
    if n is None:
        return None, ""
    ytd = proj.get("ytd_count") or 0
    lam = proj.get("lambda_remaining")
    if lam is None:
        return None, ""
    needed = n - ytd
    p = p_at_least(lam, needed)
    if p is None:
        return None, ""
    rationale = (f"EONET wildfires YTD {ytd} + Poisson(lambda={lam}) "
                 f"for {proj.get('days_remaining')}d remaining; need {needed} more")
    return p, rationale


def enrich_markets(
    markets: list[dict],
    *,
    storm_proj: Optional[dict] = None,
    quake_projections: Optional[dict] = None,
    wildfire_proj: Optional[dict] = None,
) -> list[dict]:
    """Attach _model_p / _implied_p / _edge_pp / _rationale to each market."""
    out: list[dict] = []
    for m in markets:
        title = ((m.get("_event_title") or "") + " " + (m.get("question") or "")).strip()
        tl = title.lower()
        implied = _parse_implied(m)
        model_p: Optional[float] = None
        rationale = ""

        if _is_atlantic_storm_market(tl) and storm_proj:
            model_p, rationale = _score_storm_market(tl, storm_proj)

        if model_p is None and _is_quake_market(tl) and quake_projections:
            model_p, rationale = _score_quake_market(tl, quake_projections)

        if model_p is None and _is_wildfire_market(tl) and wildfire_proj:
            model_p, rationale = _score_wildfire_market(tl, wildfire_proj)

        edge_pp: Optional[float] = None
        if implied is not None and model_p is not None:
            edge_pp = round((model_p - implied) * 100, 1)

        out.append({
            **m,
            "_implied_p": implied,
            "_model_p": round(model_p, 3) if model_p is not None else None,
            "_edge_pp": edge_pp,
            "_rationale": rationale,
        })
    # Sort: scored markets first (by absolute edge desc), unscored last
    out.sort(key=lambda r: (
        0 if r.get("_edge_pp") is not None else 1,
        -abs(r.get("_edge_pp") or 0),
    ))
    return out


if __name__ == "__main__":
    # Smoke test: a fake market
    fake_storm = {
        "lambda_remaining": 4.0,
        "active_named_storms_ytd_lower_bound": 10,
        "days_remaining_season": 30,
    }
    fake_market = {
        "_event_title": "How many Atlantic named storms in 2026?",
        "question": "At least 14 Atlantic named storms",
        "lastTradePrice": "0.65",
    }
    enriched = enrich_markets([fake_market], storm_proj=fake_storm)
    import json
    print(json.dumps(enriched, indent=2, default=str))
