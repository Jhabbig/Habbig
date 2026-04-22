"""Metaculus adapter.

Public API, no auth required:
  https://www.metaculus.com/api2/questions/?search=<terms>

Rate limit is polite but unspecified — we cap to 30 req/min per
ops guidance and bake a 2.1s spacing into the sync job.

Metaculus represents community forecasts as "community_prediction"
with a "y" array; the current consensus is last-in-list. Some older
questions only have "prediction_timeseries". We prefer the newer
field but fall back for compatibility.

Resolved questions have ``resolution`` set (0.0 or 1.0 for binary,
something else for numeric). We don't support numeric markets here —
the sync skips them.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

from external_forecasts.base import Candidate, ProviderError, clamp_probability


log = logging.getLogger("forecasts.metaculus")
_BASE = "https://www.metaculus.com/api2"
_TIMEOUT = 10.0
_MAX_CANDIDATES = 8  # cap what we ship to the matcher — keeps prompts short


async def fetch_matching(market: dict) -> list[Candidate]:
    """Return up to ``_MAX_CANDIDATES`` binary-resolution Metaculus
    questions whose search score matches the market's question text.

    Never raises — returns [] on any transport/parse failure so the
    sync job can move on to the next provider.
    """
    query = _search_query(market)
    if not query:
        return []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            r = await client.get(
                f"{_BASE}/questions/",
                params={"search": query, "limit": 20, "type": "forecast"},
                headers={"User-Agent": "narve.ai forecast-benchmark (support@narve.ai)"},
            )
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("metaculus search failed for %r: %s", query, exc)
            return []

    results = data.get("results") or []
    out: list[Candidate] = []
    for q in results:
        cand = _parse_question(q)
        if cand is None:
            continue
        out.append(cand)
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


# ── Internals ────────────────────────────────────────────────────────


def _search_query(market: dict) -> str:
    """Build a lightweight search string from our market question.
    Metaculus ranks by search score; longer queries with stopwords
    drown out the signal. We keep the 8 longest alphanumeric tokens."""
    q = str(market.get("market_question") or market.get("question") or "").strip()
    if not q:
        return ""
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", q) if len(t) > 3]
    tokens.sort(key=len, reverse=True)
    return " ".join(tokens[:8]) if tokens else q[:80]


def _parse_question(q: dict) -> Optional[Candidate]:
    qtype = q.get("possibilities", {}).get("type") or q.get("type")
    if qtype not in ("binary", "forecast"):
        # Continuous / multiple-choice — outside scope for this feature.
        return None
    prob = _probability_from(q)
    if prob is None:
        return None
    try:
        prob = clamp_probability(prob)
    except ValueError:
        return None

    qid = q.get("id")
    if qid is None:
        return None
    close_at = _parse_iso(q.get("close_time"))
    resolved = q.get("resolution") is not None

    return Candidate(
        provider="metaculus",
        provider_market_id=str(qid),
        question=str(q.get("title") or q.get("question_title") or "").strip(),
        probability=prob,
        close_at=close_at,
        resolved=bool(resolved),
        url=f"https://www.metaculus.com/questions/{qid}/",
        volume=float(q.get("number_of_predictions") or 0) or None,
    )


def _probability_from(q: dict) -> Optional[float]:
    cp = q.get("community_prediction") or {}
    full = cp.get("full") or {}
    # The freshest community number lives in full.q2 (the median).
    if isinstance(full.get("q2"), (int, float)):
        return float(full["q2"])
    # Older API shape — last element of the timeseries.
    ts = q.get("prediction_timeseries") or []
    if isinstance(ts, list) and ts:
        last = ts[-1]
        if isinstance(last, dict) and isinstance(last.get("community_prediction"), (int, float)):
            return float(last["community_prediction"])
    return None


def _parse_iso(s: Any) -> Optional[int]:
    if not s or not isinstance(s, str):
        return None
    from datetime import datetime, timezone
    try:
        s2 = s.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s2).astimezone(timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None
