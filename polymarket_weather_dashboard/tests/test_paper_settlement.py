"""Tests for the paper-order settlement loop.

The loop is what makes "working" limit orders eventually fill when
the orderbook moves. Pins the headline behaviors:
  * Working orders get re-fed the current book and fill the
    marketable portion.
  * Partial fills bump the status correctly.
  * Settlement doesn't double-spend an already-filled order.
  * Audit rows record each settlement event.
  * Missing/None orderbook is skipped without erroring.
"""

import pytest

import paper_trading as paper
import trade_engine as engine
from tests._trade_fixtures import make_in_memory_conn_factory


def _book(yes_asks=None, yes_bids=None):
    return paper.Orderbook(
        ticker="AA",
        yes_bids=[paper.OrderbookLevel(p, s) for p, s in (yes_bids or [])],
        yes_asks=[paper.OrderbookLevel(p, s) for p, s in (yes_asks or [])],
    )


def _seed_working_order(factory, user_id="u1", ticker="AA", side="yes",
                       action="buy", qty=10, limit_price_cents=55) -> int:
    """Create a working limit order with no prior fills."""
    return paper.insert_paper_order(
        factory, user_id=user_id, ticker=ticker, side=side, action=action,
        qty=qty, limit_price_cents=limit_price_cents, type_="limit",
        status="working",
    )


def test_settlement_fills_working_order_when_book_moves():
    factory, _ = make_in_memory_conn_factory()
    oid = _seed_working_order(factory, qty=10, limit_price_cents=55)
    # Initially no book → order sits at "working"
    # Now book moves to 50¢ ask: the limit at 55¢ becomes marketable
    book = _book(yes_asks=[(50, 10)])
    stats = engine.settle_working_orders(factory, lambda t: book)
    assert stats["filled_orders"] == 1
    assert stats["new_fills"] == 1
    # Order should now be filled
    orders = paper.list_paper_orders(factory, "u1")
    assert orders[0]["status"] == "filled"
    assert orders[0]["filled_qty"] == 10
    # Position should reflect the fill
    qty, avg = paper.get_paper_position(factory, "u1", "AA", "yes")
    assert qty == 10
    assert avg == 50


def test_settlement_records_partial_fill():
    factory, _ = make_in_memory_conn_factory()
    _seed_working_order(factory, qty=10, limit_price_cents=55)
    # Only 4 contracts available at the limit
    book = _book(yes_asks=[(54, 4), (60, 100)])
    engine.settle_working_orders(factory, lambda t: book)
    orders = paper.list_paper_orders(factory, "u1")
    assert orders[0]["status"] == "partially_filled"
    assert orders[0]["filled_qty"] == 4


def test_settlement_skips_unmarketable_order():
    factory, _ = make_in_memory_conn_factory()
    _seed_working_order(factory, qty=10, limit_price_cents=50)
    # All asks are above the limit
    book = _book(yes_asks=[(60, 100)])
    stats = engine.settle_working_orders(factory, lambda t: book)
    assert stats["new_fills"] == 0
    orders = paper.list_paper_orders(factory, "u1")
    assert orders[0]["status"] == "working"
    assert orders[0]["filled_qty"] == 0


def test_settlement_skips_when_orderbook_unavailable():
    factory, _ = make_in_memory_conn_factory()
    _seed_working_order(factory, qty=10, limit_price_cents=55)
    stats = engine.settle_working_orders(factory, lambda t: None)
    assert stats["new_fills"] == 0
    # Order still working — we didn't touch it
    orders = paper.list_paper_orders(factory, "u1")
    assert orders[0]["status"] == "working"


def test_settlement_handles_orderbook_fetch_exception():
    factory, _ = make_in_memory_conn_factory()
    _seed_working_order(factory, qty=10, limit_price_cents=55)

    def boom(_t):
        raise RuntimeError("nope")

    stats = engine.settle_working_orders(factory, boom)
    assert stats["errors"] >= 1
    # Order should still be working — error didn't corrupt state
    orders = paper.list_paper_orders(factory, "u1")
    assert orders[0]["status"] == "working"


def test_settlement_continues_filling_partial_across_passes():
    """An order partially filled in pass 1 should fill more in pass 2."""
    factory, _ = make_in_memory_conn_factory()
    _seed_working_order(factory, qty=10, limit_price_cents=55)
    # Pass 1: 4 available
    engine.settle_working_orders(factory, lambda t: _book(yes_asks=[(54, 4)]))
    orders = paper.list_paper_orders(factory, "u1")
    assert orders[0]["filled_qty"] == 4
    # Pass 2: 6 more available
    engine.settle_working_orders(factory, lambda t: _book(yes_asks=[(54, 6)]))
    orders = paper.list_paper_orders(factory, "u1")
    assert orders[0]["status"] == "filled"
    assert orders[0]["filled_qty"] == 10
    # And position is right
    qty, _ = paper.get_paper_position(factory, "u1", "AA", "yes")
    assert qty == 10


def test_settlement_writes_audit_row_per_fill_event():
    factory, _ = make_in_memory_conn_factory()
    _seed_working_order(factory)
    engine.settle_working_orders(factory, lambda t: _book(yes_asks=[(50, 10)]))
    with factory(readonly=True) as conn:
        rows = conn.execute(
            "SELECT action FROM trade_audit WHERE user_id = 'u1'"
        ).fetchall()
    assert any(r["action"] == "settled_paper" for r in rows)


def test_settlement_handles_no_open_orders():
    factory, _ = make_in_memory_conn_factory()
    stats = engine.settle_working_orders(factory, lambda t: _book())
    assert stats == {"checked": 0, "filled_orders": 0,
                     "new_fills": 0, "errors": 0}


def test_settlement_fetches_each_ticker_only_once():
    """When multiple working orders share a ticker, the loop should
    only call the orderbook fetcher once per ticker per pass — keeps
    the upstream rate-limit footprint low."""
    factory, _ = make_in_memory_conn_factory()
    _seed_working_order(factory, user_id="u1", ticker="AA")
    _seed_working_order(factory, user_id="u2", ticker="AA")
    _seed_working_order(factory, user_id="u1", ticker="BB")

    call_count = {"n": 0, "tickers": []}

    def counting_fetcher(t):
        call_count["n"] += 1
        call_count["tickers"].append(t)
        return _book()  # empty book, no fills

    engine.settle_working_orders(factory, counting_fetcher)
    # Only 2 distinct tickers → at most 2 fetches
    assert call_count["n"] == 2
    assert set(call_count["tickers"]) == {"AA", "BB"}


def test_settlement_short_position_close_via_buy_fill():
    """Settling a buy that crosses a short to long correctly books
    realized PnL on the closing portion."""
    factory, _ = make_in_memory_conn_factory()
    # Pre-seed a short position at 60¢
    paper.upsert_paper_position(factory, user_id="u1", ticker="AA",
                                side="yes", qty=-5, avg_price_cents=60)
    _seed_working_order(factory, action="buy", qty=5, limit_price_cents=55)
    # Book offers asks at 50¢ — buy should fill and close the short
    engine.settle_working_orders(factory, lambda t: _book(yes_asks=[(50, 5)]))
    qty, _avg = paper.get_paper_position(factory, "u1", "AA", "yes")
    assert qty == 0
    # Realized PnL = (avg_cost − fill_price) × closing_qty = (60-50)*5 = 50 cents
    with factory(readonly=True) as conn:
        rows = conn.execute(
            "SELECT realized_pnl_cents FROM paper_fills WHERE user_id='u1'"
        ).fetchall()
    assert rows[0]["realized_pnl_cents"] == 50
