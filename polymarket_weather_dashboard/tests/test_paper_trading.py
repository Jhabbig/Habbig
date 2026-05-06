"""Tests for the paper-trading simulator."""

import pytest

import paper_trading as paper
from tests._trade_fixtures import make_in_memory_conn_factory


def _book(yes_bids=None, yes_asks=None) -> paper.Orderbook:
    return paper.Orderbook(
        ticker="X-1",
        yes_bids=[paper.OrderbookLevel(p, s) for p, s in (yes_bids or [])],
        yes_asks=[paper.OrderbookLevel(p, s) for p, s in (yes_asks or [])],
    )


# ─── Fill simulation ──────────────────────────────────────────────────────────

def test_buy_yes_market_sweeps_top_of_book():
    book = _book(yes_asks=[(55, 5), (56, 10), (57, 20)])
    fills, unfilled = paper.simulate_fills(
        book, side="yes", action="buy", qty=10, type_="market",
    )
    assert unfilled == 0
    assert sum(f.qty for f in fills) == 10
    # Should fill first at 55¢ then at 56¢
    assert fills[0].price_cents == 55
    assert fills[1].price_cents == 56


def test_buy_yes_limit_fills_only_below_limit():
    book = _book(yes_asks=[(55, 5), (60, 100)])
    fills, unfilled = paper.simulate_fills(
        book, side="yes", action="buy", qty=10, type_="limit",
        limit_price_cents=55,
    )
    # 5 fill at 55, 5 stay unfilled (asks at 60 exceed limit)
    assert sum(f.qty for f in fills) == 5
    assert unfilled == 5


def test_buy_yes_limit_no_marketable_depth_returns_empty():
    book = _book(yes_asks=[(60, 100)])
    fills, unfilled = paper.simulate_fills(
        book, side="yes", action="buy", qty=10, type_="limit",
        limit_price_cents=55,
    )
    assert fills == []
    assert unfilled == 10


def test_sell_yes_walks_bids_high_to_low():
    book = _book(yes_bids=[(60, 5), (55, 10)])
    fills, unfilled = paper.simulate_fills(
        book, side="yes", action="sell", qty=10, type_="market",
    )
    assert unfilled == 0
    assert fills[0].price_cents == 60
    assert fills[1].price_cents == 55


def test_buy_no_walks_yes_bids():
    """Buying NO at price P maps to selling YES at (100-P): walks yes_bids."""
    book = _book(yes_bids=[(40, 5), (38, 10)])
    fills, unfilled = paper.simulate_fills(
        book, side="no", action="buy", qty=8, type_="market",
    )
    assert unfilled == 0
    # Best YES bid 40 → NO ask 60, then 38 → 62
    assert fills[0].price_cents == 60
    assert fills[1].price_cents == 62


def test_market_order_with_no_depth_returns_unfilled_remainder():
    book = _book(yes_asks=[(55, 3)])
    fills, unfilled = paper.simulate_fills(
        book, side="yes", action="buy", qty=10, type_="market",
    )
    assert sum(f.qty for f in fills) == 3
    assert unfilled == 7


def test_zero_qty_returns_empty():
    book = _book(yes_asks=[(55, 5)])
    fills, unfilled = paper.simulate_fills(
        book, side="yes", action="buy", qty=0, type_="market",
    )
    assert fills == []
    assert unfilled == 0


# ─── Position math ────────────────────────────────────────────────────────────

def test_buy_then_sell_realizes_profit():
    # Open at 50, close at 60 → profit = 10*qty cents per contract
    upd = paper.update_position_after_fill(
        prior_qty=0, prior_avg_cents=0, action="buy",
        fill_qty=10, fill_price_cents=50,
    )
    assert upd.new_qty == 10
    assert upd.avg_price_cents == 50
    assert upd.realized_pnl_cents == 0

    upd2 = paper.update_position_after_fill(
        prior_qty=10, prior_avg_cents=50, action="sell",
        fill_qty=10, fill_price_cents=60,
    )
    assert upd2.new_qty == 0
    assert upd2.realized_pnl_cents == 100  # (60-50)*10 cents


def test_partial_close_realizes_partial_profit():
    upd = paper.update_position_after_fill(
        prior_qty=10, prior_avg_cents=50, action="sell",
        fill_qty=4, fill_price_cents=70,
    )
    assert upd.new_qty == 6
    assert upd.realized_pnl_cents == 80  # (70-50)*4
    # Avg cost on remaining position unchanged
    assert upd.avg_price_cents == 50


def test_average_cost_rolls_on_consecutive_buys():
    # 10@50, then 10@60 → avg = 55
    upd = paper.update_position_after_fill(
        prior_qty=10, prior_avg_cents=50, action="buy",
        fill_qty=10, fill_price_cents=60,
    )
    assert upd.new_qty == 20
    assert upd.avg_price_cents == 55
    assert upd.realized_pnl_cents == 0


def test_close_then_flip_to_short():
    # Long 5@50; sell 8@60. First 5 close at +50 (10*5), remaining 3 open short at 60.
    upd = paper.update_position_after_fill(
        prior_qty=5, prior_avg_cents=50, action="sell",
        fill_qty=8, fill_price_cents=60,
    )
    assert upd.new_qty == -3
    assert upd.realized_pnl_cents == 50  # (60-50)*5
    assert upd.avg_price_cents == 60


def test_short_close_at_lower_price_realizes_profit():
    # Short 10@60; buy 10@40 → profit (60-40)*10 = 200
    upd = paper.update_position_after_fill(
        prior_qty=-10, prior_avg_cents=60, action="buy",
        fill_qty=10, fill_price_cents=40,
    )
    assert upd.new_qty == 0
    assert upd.realized_pnl_cents == 200


# ─── DB-touching helpers ──────────────────────────────────────────────────────

def test_full_paper_lifecycle_persists_orders_and_positions():
    factory, _ = make_in_memory_conn_factory()
    oid = paper.insert_paper_order(
        factory, user_id="u1", ticker="AA", side="yes", action="buy",
        qty=5, limit_price_cents=55, type_="limit", status="filled",
    )
    paper.insert_paper_fill(
        factory, user_id="u1", order_id=oid, ticker="AA", side="yes",
        action="buy", qty=5, price_cents=55, realized_pnl_cents=0,
    )
    paper.upsert_paper_position(
        factory, user_id="u1", ticker="AA", side="yes",
        qty=5, avg_price_cents=55,
    )
    pos = paper.get_paper_position(factory, "u1", "AA", "yes")
    assert pos == (5, 55)
    orders = paper.list_paper_orders(factory, "u1")
    assert len(orders) == 1
    positions = paper.list_paper_positions(factory, "u1")
    assert len(positions) == 1


def test_cancel_paper_order_only_works_on_open_orders():
    factory, _ = make_in_memory_conn_factory()
    oid = paper.insert_paper_order(
        factory, user_id="u1", ticker="AA", side="yes", action="buy",
        qty=1, limit_price_cents=50, type_="limit", status="working",
    )
    assert paper.cancel_paper_order(factory, "u1", oid) is True
    # Already canceled — no-op
    assert paper.cancel_paper_order(factory, "u1", oid) is False
    # Different user can't cancel
    other = paper.insert_paper_order(
        factory, user_id="u1", ticker="AA", side="yes", action="buy",
        qty=1, limit_price_cents=50, type_="limit", status="working",
    )
    assert paper.cancel_paper_order(factory, "other-user", other) is False
