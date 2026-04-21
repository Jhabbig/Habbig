"""Normalises Polymarket and Kalshi markets into a unified schema."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .kalshi_client import KalshiClient
from .polymarket_client import PolymarketClient

log = logging.getLogger("gateway.unified_markets")


class MarketSource(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class MarketStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    RESOLVED = "resolved"


@dataclass
class UnifiedMarket:
    id: str  # "poly:{slug}" or "kalshi:{ticker}"
    source: str
    title: str
    category: str
    yes_price: float  # 0.0–1.0
    no_price: float  # 1 - yes_price
    volume_usd: float
    liquidity_usd: float
    close_time: Optional[str]  # ISO format or None
    status: str  # "active", "closed", "resolved"
    outcome: Optional[str]
    url: str
    # Polymarket-specific: CLOB token IDs (YES and NO outcome tokens on Polygon)
    # Needed client-side to build EIP-712 orders. None for Kalshi markets.
    poly_yes_token_id: Optional[str] = None
    poly_no_token_id: Optional[str] = None
    poly_neg_risk: bool = False
    # betyc signal data — populated by enrich_markets_with_intelligence()
    betyc_ev_score: Optional[float] = None
    betyc_avg_credibility: Optional[float] = None
    betyc_prediction_count: int = 0
    betyc_consensus: Optional[str] = None  # "YES" | "NO" | "SPLIT"
    # False consensus detection (F5) — flagged when high market price
    # disagrees strongly with credibility-weighted intelligence.
    false_consensus: bool = False
    false_consensus_direction: Optional[str] = None  # "OVERPRICED" | "UNDERPRICED"

    def to_dict(self) -> dict:
        return asdict(self)


def _normalise_polymarket(market: dict) -> Optional[UnifiedMarket]:
    """Convert a Polymarket API market dict to UnifiedMarket."""
    try:
        slug = market.get("slug", market.get("conditionId", ""))
        if not slug:
            return None

        # Extract yes price from outcomes
        outcomes = market.get("outcomePrices", "")
        if isinstance(outcomes, str):
            # Sometimes returned as "[0.65, 0.35]"
            try:
                import json
                prices = json.loads(outcomes)
                yes_price = float(prices[0]) if prices else 0.5
            except (json.JSONDecodeError, IndexError):
                yes_price = 0.5
        elif isinstance(outcomes, list):
            yes_price = float(outcomes[0]) if outcomes else 0.5
        else:
            yes_price = 0.5

        # Determine status
        active = market.get("active", True)
        closed = market.get("closed", False)
        resolved = market.get("resolved", False)
        if resolved:
            status = MarketStatus.RESOLVED.value
        elif closed:
            status = MarketStatus.CLOSED.value
        else:
            status = MarketStatus.ACTIVE.value

        close_time = market.get("endDate") or market.get("endDateIso")

        # CLOB token IDs — required for client-side order signing.
        # Gamma API returns them as a JSON-encoded string like '["123","456"]'
        # where index 0 is the YES token and index 1 is the NO token.
        yes_token_id: Optional[str] = None
        no_token_id: Optional[str] = None
        raw_clob_ids = market.get("clobTokenIds")
        try:
            if isinstance(raw_clob_ids, str) and raw_clob_ids:
                import json as _json
                parsed = _json.loads(raw_clob_ids)
            else:
                parsed = raw_clob_ids
            if isinstance(parsed, list) and len(parsed) >= 2:
                yes_token_id = str(parsed[0]) if parsed[0] else None
                no_token_id = str(parsed[1]) if parsed[1] else None
        except (ValueError, TypeError):
            pass

        neg_risk = bool(market.get("negRisk", False))

        return UnifiedMarket(
            id=f"poly:{slug}",
            source=MarketSource.POLYMARKET.value,
            title=market.get("question", market.get("title", "Unknown")),
            category=_guess_category(market.get("question", ""), market.get("groupItemTitle", "")),
            yes_price=round(yes_price, 4),
            no_price=round(1 - yes_price, 4),
            volume_usd=float(market.get("volume", 0) or 0),
            liquidity_usd=float(market.get("liquidity", 0) or 0),
            close_time=close_time,
            status=status,
            outcome=market.get("outcome") if resolved else None,
            url=f"https://polymarket.com/event/{slug}",
            poly_yes_token_id=yes_token_id,
            poly_no_token_id=no_token_id,
            poly_neg_risk=neg_risk,
        )
    except Exception as e:
        log.warning("Failed to normalise Polymarket market: %s", e)
        return None


def _normalise_kalshi(market: dict) -> Optional[UnifiedMarket]:
    """Convert a Kalshi API market dict to UnifiedMarket."""
    try:
        ticker = market.get("ticker", "")
        if not ticker:
            return None

        yes_price = (market.get("yes_ask", 50) or 50) / 100.0
        status_raw = market.get("status", "open")
        if status_raw in ("settled", "finalized"):
            status = MarketStatus.RESOLVED.value
        elif status_raw == "closed":
            status = MarketStatus.CLOSED.value
        else:
            status = MarketStatus.ACTIVE.value

        close_time = market.get("close_time") or market.get("expiration_time")

        return UnifiedMarket(
            id=f"kalshi:{ticker}",
            source=MarketSource.KALSHI.value,
            title=market.get("title", market.get("subtitle", "Unknown")),
            category=_guess_category(market.get("title", ""), market.get("category", "")),
            yes_price=round(yes_price, 4),
            no_price=round(1 - yes_price, 4),
            volume_usd=float(market.get("volume", 0) or 0),
            liquidity_usd=float(market.get("open_interest", 0) or 0),
            close_time=close_time,
            status=status,
            outcome=market.get("result") if status == MarketStatus.RESOLVED.value else None,
            url=f"https://kalshi.com/markets/{ticker}",
        )
    except Exception as e:
        log.warning("Failed to normalise Kalshi market: %s", e)
        return None


# Category keywords for heuristic classification
_CATEGORY_KEYWORDS = {
    "politics": ["election", "president", "senate", "congress", "democrat", "republican", "trump", "biden", "vote", "political", "governor", "ballot"],
    "sports": ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball", "baseball", "tennis", "sports", "super bowl", "world cup", "champion"],
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain", "token", "altcoin", "defi", "solana"],
    "finance": ["stock", "market", "fed", "interest rate", "gdp", "inflation", "recession", "s&p", "nasdaq", "dow"],
    "weather": ["weather", "temperature", "rain", "snow", "hurricane", "tornado", "climate", "flood"],
    "entertainment": ["oscar", "grammy", "movie", "tv", "netflix", "spotify", "album", "box office"],
    "science": ["ai", "artificial intelligence", "spacex", "nasa", "science", "research", "fda", "vaccine"],
    "world": ["war", "conflict", "china", "russia", "ukraine", "geopolitical", "nato", "un"],
}


def _guess_category(title: str, extra: str = "") -> str:
    """Heuristically classify a market into a category."""
    text = f"{title} {extra}".lower()
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return cat
    return "other"


# ── Simple in-memory cache ──────────────────────────────────────────────────

_cache: dict[str, tuple[float, object]] = {}


def _get_cached(key: str, ttl: int) -> Optional[object]:
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < ttl:
            return data
    return None


def _set_cached(key: str, data: object) -> None:
    _cache[key] = (time.time(), data)


# ── Unified fetch ───────────────────────────────────────────────────────────


async def fetch_unified_markets(
    poly_client: PolymarketClient,
    kalshi_client: KalshiClient,
    *,
    cache_ttl: int = 300,
) -> list[UnifiedMarket]:
    """Fetch and merge markets from both sources. Cached for cache_ttl seconds."""
    cached = _get_cached("unified_markets", cache_ttl)
    if cached is not None:
        return cached

    poly_raw, kalshi_raw = await asyncio.gather(
        poly_client.get_all_markets(),
        kalshi_client.get_all_markets(),
        return_exceptions=True,
    )

    markets: list[UnifiedMarket] = []

    if isinstance(poly_raw, list):
        for m in poly_raw:
            normalised = _normalise_polymarket(m)
            if normalised:
                markets.append(normalised)
    else:
        log.error("Polymarket fetch failed: %s", poly_raw)

    if isinstance(kalshi_raw, list):
        for m in kalshi_raw:
            normalised = _normalise_kalshi(m)
            if normalised:
                markets.append(normalised)
    else:
        log.error("Kalshi fetch failed: %s", kalshi_raw)

    _set_cached("unified_markets", markets)
    return markets


async def fetch_single_market(
    poly_client: PolymarketClient,
    kalshi_client: KalshiClient,
    market_id: str,
    *,
    cache_ttl: int = 120,
) -> Optional[UnifiedMarket]:
    """Fetch a single market by unified ID (poly:{slug} or kalshi:{ticker})."""
    cached = _get_cached(f"market:{market_id}", cache_ttl)
    if cached is not None:
        return cached

    if market_id.startswith("poly:"):
        slug = market_id[5:]
        raw = await poly_client.get_market(slug)
        if raw:
            result = _normalise_polymarket(raw)
            if result:
                _set_cached(f"market:{market_id}", result)
                return result
    elif market_id.startswith("kalshi:"):
        ticker = market_id[7:]
        raw = await kalshi_client.get_market(ticker)
        if raw:
            result = _normalise_kalshi(raw)
            if result:
                _set_cached(f"market:{market_id}", result)
                return result

    return None


def filter_markets(
    markets: list[UnifiedMarket],
    *,
    category: str = "",
    source: str = "",
    search: str = "",
    sort: str = "volume",
) -> list[UnifiedMarket]:
    """Filter and sort a list of unified markets."""
    result = markets

    if category:
        result = [m for m in result if m.category == category]

    if source:
        result = [m for m in result if m.source == source]

    if search:
        q = search.lower()
        result = [m for m in result if q in m.title.lower()]

    # Sort
    if sort == "ev":
        result.sort(key=lambda m: abs(m.betyc_ev_score or 0), reverse=True)
    elif sort == "volume":
        result.sort(key=lambda m: m.volume_usd, reverse=True)
    elif sort == "close_time":
        result.sort(key=lambda m: m.close_time or "9999", reverse=False)
    elif sort == "credibility":
        result.sort(key=lambda m: m.betyc_avg_credibility or 0, reverse=True)

    return result


# ── Intelligence enrichment (F4 + F5) ──────────────────────────────────────


# Cache enriched results separately from raw market data so the 5-minute
# market cache doesn't force a full re-enrichment on every request.
_ENRICHMENT_CACHE_KEY = "enriched_markets"
_ENRICHMENT_TTL = 120  # 2 minutes — lighter than market fetch


def enrich_markets_with_intelligence(markets: list[UnifiedMarket]) -> list[UnifiedMarket]:
    """Populate betyc_* and false_consensus fields using prediction data.

    For each market with predictions in the DB:
      1. Call calculate_betyc_probability to get a credibility-weighted consensus.
      2. Compute edge: betyc_probability - market_yes_price.
      3. Detect false consensus: market extreme (>80% or <20%) but credibility
         intelligence disagrees by >15 percentage points.

    Non-async because the DB queries are fast in-process SQLite reads.
    Runs against all active markets and caches the result for 2 minutes.
    """
    import db

    cached = _get_cached(_ENRICHMENT_CACHE_KEY, _ENRICHMENT_TTL)
    if cached is not None:
        return cached

    for market in markets:
        try:
            preds = db.get_predictions_for_market(market.id)
            if not preds:
                continue

            pred_dicts = [
                {
                    "source_handle": p["source_handle"],
                    "direction": p["direction"],
                    "predicted_probability": p["predicted_probability"],
                    "global_credibility": p["global_credibility"],
                    "category_credibility": p["category_credibility"] if "category_credibility" in p.keys() else None,
                    "accuracy_unlocked": bool(p["accuracy_unlocked"]) if p["accuracy_unlocked"] is not None else False,
                }
                for p in preds
            ]

            result = db.calculate_betyc_probability(pred_dicts)
            market.betyc_prediction_count = result["betyc_source_count"]

            if result["betyc_yes_probability"] is not None:
                market.betyc_ev_score = round(
                    result["betyc_yes_probability"] - market.yes_price, 4
                )
                avg_cred = sum(
                    d.get("global_credibility") or 0.5 for d in pred_dicts
                ) / max(len(pred_dicts), 1)
                market.betyc_avg_credibility = round(avg_cred, 4)

                betyc_yes = result["betyc_yes_probability"]
                if betyc_yes > 0.55:
                    market.betyc_consensus = "YES"
                elif betyc_yes < 0.45:
                    market.betyc_consensus = "NO"
                else:
                    market.betyc_consensus = "SPLIT"

                # ── False consensus detection (F5) ──────────────────────
                market_extreme = market.yes_price > 0.80 or market.yes_price < 0.20
                strong_disagreement = abs(market.betyc_ev_score) > 0.15
                if market_extreme and strong_disagreement:
                    market.false_consensus = True
                    market.false_consensus_direction = (
                        "OVERPRICED" if market.betyc_ev_score < -0.15
                        else "UNDERPRICED"
                    )
        except Exception as exc:
            log.warning("Enrichment failed for %s: %s", market.id, exc)

    _set_cached(_ENRICHMENT_CACHE_KEY, markets)
    return markets


# ── Kelly criterion bet sizing (F16) ────────────────────────────────────────


def compute_kelly_sizing(
    betyc_probability: float,
    market_yes_price: float,
    bankroll: float,
    fraction: float = 0.5,
    max_cap: float = 0.25,
) -> dict:
    """Kelly criterion position sizing.

    Kelly formula: f* = (p * b - q) / b
    where p = true probability, q = 1-p, b = payout ratio = (1/price - 1)

    Args:
        betyc_probability: narve.ai's credibility-weighted probability (0-1)
        market_yes_price: current market YES price (0-1)
        bankroll: user's stated bankroll in USD
        fraction: Kelly fraction (0.5 = half-Kelly, 1.0 = full Kelly)
        max_cap: upper bound on *full* Kelly (default 25%). Protects against
            huge-edge recommendations that would still be ruinous if our
            probability estimate is off. `fraction` is applied after the cap.

    Returns dict with kelly fractions and recommended bet amount.
    """
    if market_yes_price <= 0 or market_yes_price >= 1 or bankroll <= 0:
        return {"kelly_full_fraction": 0, "kelly_adjusted_fraction": 0,
                "recommended_amount": 0, "edge": 0, "fraction_used": fraction}

    # Determine which side to bet
    edge = betyc_probability - market_yes_price
    if abs(edge) < 0.01:  # negligible edge
        return {"kelly_full_fraction": 0, "kelly_adjusted_fraction": 0,
                "recommended_amount": 0, "edge": round(edge, 4), "fraction_used": fraction}

    # Bet YES if edge > 0, NO if edge < 0
    if edge > 0:
        p = betyc_probability
        odds_price = market_yes_price
        side = "YES"
    else:
        p = 1 - betyc_probability
        odds_price = 1 - market_yes_price
        side = "NO"

    q = 1 - p
    b = (1 / odds_price) - 1 if odds_price > 0 else 0

    kelly_full = (p * b - q) / b if b > 0 else 0
    kelly_full = max(0.0, min(kelly_full, max_cap))  # cap before applying fraction
    kelly_adjusted = kelly_full * fraction
    recommended = round(bankroll * kelly_adjusted, 2)

    return {
        "side": side,
        "kelly_full_fraction": round(kelly_full, 4),
        "kelly_adjusted_fraction": round(kelly_adjusted, 4),
        "recommended_amount": recommended,
        "edge": round(edge, 4),
        "fraction_used": fraction,
    }
