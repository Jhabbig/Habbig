"""Paper-trade qualification + settlement tests."""
from __future__ import annotations

import pytest

from app.models import PaperTrade, Prediction, Source
from app.processing.paper_trade import (
    TradeFilter,
    _entry_price,
    backtest,
    maybe_open_trade,
    qualifies,
    settle_pnl,
    settle_trades_for_market,
    summary,
)
from app.tests.conftest import NOW


def _src(**kw):
    base = dict(handle="alice", platform="twitter", global_credibility=0.7, qualifying_predictions=15, accuracy_unlocked=True, verified=True, follower_count=10000)
    base.update(kw)
    s = Source(**base)
    s.categories_predicted_in = ["politics", "crypto", "sports"]
    s.category_credibility = {"politics": 0.7, "crypto": 0.7, "sports": 0.6}
    return s


def _pred(**kw):
    base = dict(raw_post_id="t:1", category="politics", predicted_outcome="Yes", market_slug="will-x-happen", market_implied_probability=0.45, ev_score=0.20, bet_side="YES", risk_flag=False)
    base.update(kw)
    return Prediction(extracted_at=NOW, **base)


def test_entry_price_yes(): assert _entry_price(0.45, "YES") == 0.45
def test_entry_price_no(): assert abs(_entry_price(0.45, "NO") - 0.55) < 1e-9


def test_settle_pnl_winning_yes_at_quarter():
    # $1 stake at price 0.25 -> 4 shares -> $4 payout if win -> +$3 profit
    assert abs(settle_pnl(1.0, 0.25, True) - 3.0) < 1e-9


def test_settle_pnl_losing_returns_minus_stake():
    assert settle_pnl(2.5, 0.25, False) == -2.5


def test_qualifies_passes_clean():
    assert qualifies(_pred(), _src(), TradeFilter()) is True


def test_qualifies_blocks_low_ev():
    assert qualifies(_pred(ev_score=0.05), _src(), TradeFilter()) is False


def test_qualifies_blocks_risk_flag():
    assert qualifies(_pred(risk_flag=True), _src(), TradeFilter()) is False


def test_qualifies_blocks_low_credibility():
    assert qualifies(_pred(), _src(global_credibility=0.30), TradeFilter()) is False


def test_qualifies_blocks_unrated_when_required():
    assert qualifies(_pred(), _src(accuracy_unlocked=False), TradeFilter()) is False


def test_qualifies_allows_unrated_when_filter_off():
    filt = TradeFilter(require_unlocked=False, min_credibility=0.0)
    assert qualifies(_pred(), _src(accuracy_unlocked=False), filt) is True


def test_qualifies_blocks_extreme_market():
    assert qualifies(_pred(market_implied_probability=0.999), _src(), TradeFilter()) is False


@pytest.mark.asyncio
async def test_open_and_settle_winning_yes_trade(session):
    src = _src()
    session.add(src)
    pred = _pred(id=None, ev_score=0.30, market_implied_probability=0.40)
    session.add(pred)
    await session.commit()
    await session.refresh(pred)

    trade = await maybe_open_trade(session, pred, src, "alice", TradeFilter())
    await session.commit()
    assert trade is not None
    assert trade.bet_side == "YES"
    assert abs(trade.entry_price - 0.40) < 1e-9

    settled = await settle_trades_for_market(session, pred.market_slug, won_yes=True)
    await session.commit()
    assert settled == 1
    await session.refresh(trade)
    assert trade.resolved is True
    assert trade.resolved_correct is True
    # $1 stake at 0.40 -> 2.5 shares -> $2.50 payout -> +$1.50 profit
    assert abs((trade.pnl_usd or 0) - 1.5) < 1e-9


@pytest.mark.asyncio
async def test_dedups_open_trades_for_same_market_and_handle(session):
    src = _src()
    session.add(src)
    pred = _pred(id=None)
    session.add(pred)
    await session.commit()
    await session.refresh(pred)

    first = await maybe_open_trade(session, pred, src, "alice", TradeFilter())
    await session.commit()
    second = await maybe_open_trade(session, pred, src, "alice", TradeFilter())
    await session.commit()
    assert first is not None and second is None


@pytest.mark.asyncio
async def test_summary_counts_open_and_settled(session):
    src = _src()
    session.add(src)
    p1 = _pred(id=None, market_slug="m1", ev_score=0.20)
    p2 = _pred(id=None, market_slug="m2", ev_score=0.20)
    session.add_all([p1, p2])
    await session.commit()
    await session.refresh(p1)
    await session.refresh(p2)

    await maybe_open_trade(session, p1, src, "alice", TradeFilter())
    await maybe_open_trade(session, p2, src, "alice", TradeFilter())
    await session.commit()

    await settle_trades_for_market(session, "m1", won_yes=True)
    await session.commit()

    s = await summary(session)
    assert s["open"] == 1
    assert s["settled"] == 1
    assert s["wins"] == 1


@pytest.mark.asyncio
async def test_backtest_replay_finds_resolved_predictions(session):
    from app.models import RawPost, SourcePredictionRecord

    src = _src(global_credibility=0.7)
    session.add(src)
    rp = RawPost(id="t:99", platform="twitter", author_handle="alice", content="text" * 5)
    session.add(rp)
    pred = _pred(id=None, raw_post_id="t:99", market_slug="m99", market_implied_probability=0.40, ev_score=0.30, resolved=True, resolved_correct=True)
    session.add(pred)
    await session.commit()
    await session.refresh(pred)
    spr = SourcePredictionRecord(handle="alice", prediction_id=pred.id, market_slug="m99", category="politics")
    session.add(spr)
    await session.commit()

    result = await backtest(session, TradeFilter())
    assert result["n_signals"] == 1
    assert result["wins"] == 1
    assert abs(result["total_pnl_usd"] - 1.5) < 1e-9
