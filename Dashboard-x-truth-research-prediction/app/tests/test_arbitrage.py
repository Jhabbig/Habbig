"""Cross-venue arbitrage matching + edge detection tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import MarketSnapshot
from app.processing.arbitrage import _match_score, find_arbs

NOW = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _ms(slug, question, yes, platform="polymarket", category="politics", volume=10000.0, close=NOW + timedelta(days=30)):
    return MarketSnapshot(
        market_slug=slug, market_question=question, category=category,
        yes_price=yes, volume_usd=volume, close_time=close,
        platform=platform, snapshotted_at=NOW,
    )


def test_match_score_strips_template_filler():
    """Two markets with identical content tokens should match even though
    'Will X happen by Y?' template words are stripped from both."""
    a = "Will Trump win the 2028 presidential election?"
    b = "Trump 2028 presidential election outcome?"
    score = _match_score(a, b)
    assert score >= 0.5


def test_match_score_low_for_unrelated_markets():
    a = "Will Trump win the 2028 election?"
    b = "Will the Lakers win the 2026 championship?"
    score = _match_score(a, b)
    assert score < 0.3


@pytest.mark.asyncio
async def test_find_arbs_returns_high_edge_pairs(session):
    session.add_all([
        _ms("trump-2028-poly", "Will Trump win the 2028 presidential election?", 0.55, platform="polymarket"),
        _ms("trump-2028-kalshi", "Trump 2028 presidential election outcome?", 0.48, platform="kalshi"),
    ])
    await session.commit()

    arbs = await find_arbs(session, min_edge_pp=3.0)
    assert len(arbs) == 1
    a = arbs[0]
    assert a.polymarket_slug == "trump-2028-poly"
    assert a.kalshi_ticker == "trump-2028-kalshi"
    assert abs(a.edge_pp - 7.0) < 0.01  # 0.55 - 0.48 = 7pp
    assert a.cheaper_venue == "kalshi"  # kalshi has lower YES price


@pytest.mark.asyncio
async def test_find_arbs_skips_under_threshold(session):
    session.add_all([
        _ms("a-poly", "Will Trump win the 2028 presidential election?", 0.55, platform="polymarket"),
        _ms("a-kalshi", "Trump 2028 presidential election outcome?", 0.54, platform="kalshi"),  # only 1pp apart
    ])
    await session.commit()
    arbs = await find_arbs(session, min_edge_pp=3.0)
    assert arbs == []


@pytest.mark.asyncio
async def test_find_arbs_requires_same_category(session):
    # Same wording but tagged different categories -> no match.
    session.add_all([
        _ms("a-poly", "Will Lakers win championship", 0.55, platform="polymarket", category="sports"),
        _ms("a-kalshi", "Will Lakers win championship", 0.30, platform="kalshi", category="other"),
    ])
    await session.commit()
    arbs = await find_arbs(session, min_edge_pp=3.0)
    assert arbs == []


@pytest.mark.asyncio
async def test_find_arbs_skips_far_apart_close_times(session):
    """Two markets with the same title but resolving a year apart are
    different events (e.g. annual recurring) — should not match."""
    session.add_all([
        _ms("a-poly", "Will Trump win the 2028 presidential election?", 0.55,
            platform="polymarket", close=NOW + timedelta(days=30)),
        _ms("a-kalshi", "Trump 2028 presidential election outcome?", 0.30,
            platform="kalshi", close=NOW + timedelta(days=400)),  # >14 days apart
    ])
    await session.commit()
    arbs = await find_arbs(session, min_edge_pp=3.0, close_time_tolerance_days=14)
    assert arbs == []


@pytest.mark.asyncio
async def test_find_arbs_sorted_largest_edge_first(session):
    session.add_all([
        _ms("trump-poly", "Will Trump win the 2028 presidential election?", 0.55, platform="polymarket"),
        _ms("trump-kalshi", "Trump 2028 presidential election outcome?", 0.50, platform="kalshi"),  # 5pp
        _ms("biden-poly", "Will Biden win the 2028 Democratic primary?", 0.30, platform="polymarket"),
        _ms("biden-kalshi", "Biden 2028 Democratic primary outcome?", 0.15, platform="kalshi"),  # 15pp
    ])
    await session.commit()
    arbs = await find_arbs(session, min_edge_pp=3.0)
    assert len(arbs) == 2
    assert arbs[0].edge_pp > arbs[1].edge_pp  # sorted descending
