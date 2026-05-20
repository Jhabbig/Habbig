"""Cross-venue arbitrage detector.

Same political/sports/crypto event often trades on Polymarket *and* Kalshi with
a 3–10 percentage-point spread. A trader who can place orders on both venues
can lock in a risk-free return: buy YES on the cheap venue, buy NO on the
expensive one (or equivalently, buy YES + sell-equivalent on the other side).

This module matches markets across venues by their category + Jaccard
token-overlap on question titles, computes the YES-side spread, and surfaces
opportunities >3pp.

Matching is conservative: we require ≥60% Jaccard similarity and same
category, which is much stricter than the prediction-to-market matcher
(0.50 threshold) — false positives here mean phantom arb opportunities.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlmodel import select

from app.config import yaml_config
from app.db import AsyncSession
from app.models import MarketSnapshot
from app.processing.extractor import _jaccard, _tokenize

logger = logging.getLogger(__name__)


_ARB_CFG = yaml_config.get("arbitrage", {}) or {}
DEFAULT_MATCH_THRESHOLD: float = float(_ARB_CFG.get("match_threshold", 0.60))
DEFAULT_MIN_EDGE_PP: float = float(_ARB_CFG.get("min_edge_pp", 3.0))  # 3 percentage points
DEFAULT_CLOSE_TIME_TOLERANCE_DAYS: int = int(_ARB_CFG.get("close_time_tolerance_days", 14))


@dataclass
class ArbOpportunity:
    """One matched market pair where the YES prices differ enough to be tradeable."""
    polymarket_slug: str
    kalshi_ticker: str
    question: str  # the Polymarket question (canonical for display)
    kalshi_title: str
    category: str
    poly_yes: float
    kalshi_yes: float
    edge_pp: float  # |poly_yes - kalshi_yes| × 100, signed so positive means BUY cheap venue
    cheaper_venue: str  # "polymarket" or "kalshi" — where to buy YES
    polymarket_volume: float
    kalshi_volume: float
    match_score: float
    close_time_poly: Optional[str]
    close_time_kalshi: Optional[str]


def _normalize_question_tokens(q: str) -> set[str]:
    """Tokenise + drop common market-question filler words that would inflate
    Jaccard between unrelated markets (e.g. "Will X happen by Y?" templates)."""
    tokens = _tokenize(q)
    filler = {"will", "happen", "by", "before", "after", "year", "month", "day", "next", "first", "win", "be"}
    return tokens - filler


def _match_score(poly_q: str, kalshi_q: str) -> float:
    """Stricter Jaccard for cross-venue matching — strips template filler so
    we don't match every "Will X happen by Y?" market to every other one."""
    a = _normalize_question_tokens(poly_q)
    b = _normalize_question_tokens(kalshi_q)
    return _jaccard(a, b)


async def find_arbs(
    session: AsyncSession,
    match_threshold: float = DEFAULT_MATCH_THRESHOLD,
    min_edge_pp: float = DEFAULT_MIN_EDGE_PP,
    close_time_tolerance_days: int = DEFAULT_CLOSE_TIME_TOLERANCE_DAYS,
) -> list[ArbOpportunity]:
    """Match Polymarket and Kalshi markets, return arbs above ``min_edge_pp``.

    We keep at most one (poly, kalshi) pair per Polymarket slug — the
    highest-scoring Kalshi match. Pre-grouping by category cuts the matching
    cost from O(P×K) to O(sum of in-category products).
    """
    # Group active markets by category, per platform.
    result = await session.exec(select(MarketSnapshot))
    all_markets = result.all()
    poly_by_cat: dict[str, list[MarketSnapshot]] = {}
    kalshi_by_cat: dict[str, list[MarketSnapshot]] = {}
    for m in all_markets:
        if (m.platform or "polymarket") == "polymarket":
            poly_by_cat.setdefault(m.category or "other", []).append(m)
        elif m.platform == "kalshi":
            kalshi_by_cat.setdefault(m.category or "other", []).append(m)

    arbs: list[ArbOpportunity] = []
    seen_pairs: set[tuple[str, str]] = set()

    for category, poly_list in poly_by_cat.items():
        kalshi_list = kalshi_by_cat.get(category, [])
        if not kalshi_list:
            continue
        for poly in poly_list:
            best_match: tuple[Optional[MarketSnapshot], float] = (None, 0.0)
            for kalshi in kalshi_list:
                # Skip pairs that close far apart in time — likely different events
                # even with similar titles (e.g. annual recurring markets).
                if poly.close_time and kalshi.close_time:
                    gap = abs((poly.close_time - kalshi.close_time).total_seconds())
                    if gap > close_time_tolerance_days * 86400:
                        continue
                score = _match_score(poly.market_question, kalshi.market_question)
                if score > best_match[1]:
                    best_match = (kalshi, score)
            kalshi, score = best_match
            if kalshi is None or score < match_threshold:
                continue
            pair_key = (poly.market_slug, kalshi.market_slug)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            edge_signed = (poly.yes_price - kalshi.yes_price) * 100.0
            edge_abs = abs(edge_signed)
            if edge_abs < min_edge_pp:
                continue
            cheaper = "kalshi" if poly.yes_price > kalshi.yes_price else "polymarket"
            arbs.append(ArbOpportunity(
                polymarket_slug=poly.market_slug,
                kalshi_ticker=kalshi.market_slug,
                question=poly.market_question or "",
                kalshi_title=kalshi.market_question or "",
                category=category,
                poly_yes=round(poly.yes_price, 4),
                kalshi_yes=round(kalshi.yes_price, 4),
                edge_pp=round(edge_abs, 2),
                cheaper_venue=cheaper,
                polymarket_volume=round(poly.volume_usd or 0.0, 2),
                kalshi_volume=round(kalshi.volume_usd or 0.0, 2),
                match_score=round(score, 3),
                close_time_poly=poly.close_time.isoformat() if poly.close_time else None,
                close_time_kalshi=kalshi.close_time.isoformat() if kalshi.close_time else None,
            ))
    # Sort largest edges first.
    arbs.sort(key=lambda a: a.edge_pp, reverse=True)
    return arbs
