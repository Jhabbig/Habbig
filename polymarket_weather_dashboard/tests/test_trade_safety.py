"""Tests for the pre-trade safety layer."""

import pytest

import trade_safety as safety
from tests._trade_fixtures import make_in_memory_conn_factory


# ─── Sanity checks (pure) ─────────────────────────────────────────────────────

def test_sanity_accepts_valid_limit_order():
    d = safety.check_sanity(ticker="KXHIGHNY-26MAY07-T75", side="yes",
                            action="buy", qty=1, limit_price_cents=55,
                            type_="limit")
    assert d.allow is True


def test_sanity_rejects_bad_ticker():
    assert not safety.check_sanity(ticker="", side="yes", action="buy",
                                    qty=1, limit_price_cents=50).allow
    assert not safety.check_sanity(ticker="bad ticker with spaces", side="yes",
                                    action="buy", qty=1, limit_price_cents=50).allow


def test_sanity_rejects_bad_side_action_type():
    assert not safety.check_sanity(ticker="OK-1", side="maybe", action="buy",
                                    qty=1, limit_price_cents=50).allow
    assert not safety.check_sanity(ticker="OK-1", side="yes", action="hodl",
                                    qty=1, limit_price_cents=50).allow
    assert not safety.check_sanity(ticker="OK-1", side="yes", action="buy",
                                    qty=1, limit_price_cents=50, type_="iceberg").allow


def test_sanity_rejects_zero_or_negative_qty():
    assert not safety.check_sanity(ticker="OK-1", side="yes", action="buy",
                                    qty=0, limit_price_cents=50).allow
    assert not safety.check_sanity(ticker="OK-1", side="yes", action="buy",
                                    qty=-1, limit_price_cents=50).allow


def test_sanity_rejects_qty_above_ceiling():
    assert not safety.check_sanity(ticker="OK-1", side="yes", action="buy",
                                    qty=1_000_000, limit_price_cents=50).allow


def test_sanity_rejects_out_of_range_price():
    assert not safety.check_sanity(ticker="OK-1", side="yes", action="buy",
                                    qty=1, limit_price_cents=0).allow
    assert not safety.check_sanity(ticker="OK-1", side="yes", action="buy",
                                    qty=1, limit_price_cents=100).allow
    assert not safety.check_sanity(ticker="OK-1", side="yes", action="buy",
                                    qty=1, limit_price_cents=999).allow


def test_sanity_market_order_does_not_need_price():
    d = safety.check_sanity(ticker="OK-1", side="yes", action="buy",
                            qty=1, type_="market")
    assert d.allow is True


def test_sanity_limit_order_requires_price():
    d = safety.check_sanity(ticker="OK-1", side="yes", action="buy",
                            qty=1, type_="limit", limit_price_cents=None)
    assert not d.allow
    assert d.code == "bad_price"


# ─── Per-user limits ──────────────────────────────────────────────────────────

def test_user_limits_default_when_no_row():
    factory, _ = make_in_memory_conn_factory()
    lim = safety.get_user_limits(factory, "user-1")
    assert lim.user_id == "user-1"
    assert lim.killed is False
    assert lim.max_order_usd == safety.DEFAULT_MAX_ORDER_USD


def test_kill_switch_blocks_orders():
    factory, _ = make_in_memory_conn_factory()
    safety.set_user_limits(factory, "user-1", killed=1, kill_reason="admin pause")
    d = safety.check_user(factory, user_id="user-1", ticker="X-1",
                          qty=1, limit_price_cents=50, action="buy")
    assert not d.allow
    assert d.code == "killed"
    assert "admin pause" in d.reason


def test_max_order_cap():
    factory, _ = make_in_memory_conn_factory()
    safety.set_user_limits(factory, "user-1", max_order_usd=10.0,
                            max_daily_usd=1000.0)
    # 100 contracts at 50¢ = $50 — over the $10 cap
    d = safety.check_user(factory, user_id="user-1", ticker="X-1",
                          qty=100, limit_price_cents=50, action="buy")
    assert not d.allow
    assert d.code == "over_order_cap"


def test_max_order_cap_passes_within_limit():
    factory, _ = make_in_memory_conn_factory()
    safety.set_user_limits(factory, "user-1", max_order_usd=100.0,
                            max_daily_usd=1000.0)
    d = safety.check_user(factory, user_id="user-1", ticker="X-1",
                          qty=10, limit_price_cents=50, action="buy")
    assert d.allow


def test_user_self_can_lower_limits():
    factory, _ = make_in_memory_conn_factory()
    safety.set_user_limits(factory, "user-1", max_order_usd=50.0)
    safety.set_user_limits(factory, "user-1", max_order_usd=10.0)  # tighten
    lim = safety.get_user_limits(factory, "user-1")
    assert lim.max_order_usd == 10.0


def test_no_user_id_blocks():
    factory, _ = make_in_memory_conn_factory()
    d = safety.check_user(factory, user_id="", ticker="X-1", qty=1,
                          limit_price_cents=50, action="buy")
    assert not d.allow
    assert d.code == "no_user"


def test_open_position_count_only_blocks_buys():
    """Selling to close should not be blocked by the position count cap."""
    factory, conn = make_in_memory_conn_factory()
    safety.set_user_limits(factory, "user-1", max_open_positions=1)
    # Insert one open position
    conn.execute(
        """INSERT INTO paper_positions (user_id, ticker, side, qty,
                                         avg_price_cents, updated_at)
           VALUES ('user-1', 'A', 'yes', 5, 60, '2026-05-06T00:00:00Z')""")
    conn.commit()
    # Buy on a new market should be blocked
    d = safety.check_user(factory, user_id="user-1", ticker="B",
                          qty=1, limit_price_cents=50, action="buy")
    assert not d.allow
    assert d.code == "too_many_positions"
    # Sell should NOT be blocked
    d2 = safety.check_user(factory, user_id="user-1", ticker="A",
                           qty=1, limit_price_cents=50, action="sell")
    assert d2.allow
