"""Engine-level tests covering the full place-order pipeline.

Live mode uses a monkeypatched `KalshiSignedClient` that records the
call instead of hitting Kalshi — so we can verify the engine reaches
the live path with the right body without actually firing an order.
"""

import pytest

import paper_trading as paper
import trade_engine as engine
import trade_safety as safety
from tests._trade_fixtures import (
    make_in_memory_conn_factory,
    make_test_rsa_key,
)


@pytest.fixture(autouse=True)
def isolated_secret_key(tmp_path, monkeypatch):
    monkeypatch.setenv("WEATHER_SECRET_KEY_PATH", str(tmp_path / ".secret"))
    yield


def _book(yes_asks=None, yes_bids=None):
    return paper.Orderbook(
        ticker="AA",
        yes_bids=[paper.OrderbookLevel(p, s) for p, s in (yes_bids or [])],
        yes_asks=[paper.OrderbookLevel(p, s) for p, s in (yes_asks or [])],
    )


# ─── Paper path ───────────────────────────────────────────────────────────────

def test_paper_order_fills_from_book():
    factory, _ = make_in_memory_conn_factory()
    book = _book(yes_asks=[(55, 10)])
    res = engine.place_order(
        factory, user_id="u1", ticker="AA", side="yes", action="buy",
        qty=5, type_="limit", limit_price_cents=55, mode="paper",
        orderbook=book,
    )
    assert res.ok
    assert res.status == "filled"
    assert sum(f["qty"] for f in res.fills) == 5
    # Audit row written
    with factory(readonly=True) as conn:
        rows = conn.execute(
            "SELECT action FROM trade_audit WHERE user_id = 'u1'"
        ).fetchall()
    assert any(r["action"] == "placed_paper" for r in rows)


def test_paper_order_with_no_book_records_working():
    factory, _ = make_in_memory_conn_factory()
    res = engine.place_order(
        factory, user_id="u1", ticker="AA", side="yes", action="buy",
        qty=5, type_="limit", limit_price_cents=55, mode="paper",
        orderbook=None,
    )
    assert res.ok
    assert res.status == "working"
    assert res.fills == []


def test_market_order_rejects_when_depth_missing():
    factory, _ = make_in_memory_conn_factory()
    book = _book(yes_asks=[(55, 2)])
    res = engine.place_order(
        factory, user_id="u1", ticker="AA", side="yes", action="buy",
        qty=10, type_="market", mode="paper", orderbook=book,
    )
    assert not res.ok
    assert res.code == "no_depth"


def test_safety_rejection_writes_audit():
    factory, _ = make_in_memory_conn_factory()
    res = engine.place_order(
        factory, user_id="u1", ticker="bad ticker", side="yes",
        action="buy", qty=1, limit_price_cents=50, mode="paper",
        orderbook=_book(yes_asks=[(50, 5)]),
    )
    assert not res.ok
    assert res.code == "bad_ticker"
    with factory(readonly=True) as conn:
        rows = conn.execute(
            "SELECT action, detail FROM trade_audit WHERE user_id='u1'"
        ).fetchall()
    assert any(r["action"] == "rejected" for r in rows)


def test_killed_user_blocked_from_placing():
    factory, _ = make_in_memory_conn_factory()
    safety.set_user_limits(factory, "u1", killed=1, kill_reason="test")
    res = engine.place_order(
        factory, user_id="u1", ticker="AA", side="yes", action="buy",
        qty=1, limit_price_cents=50, mode="paper",
        orderbook=_book(yes_asks=[(50, 5)]),
    )
    assert not res.ok
    assert res.code == "killed"


def test_cancel_paper_order_via_engine():
    factory, _ = make_in_memory_conn_factory()
    res = engine.place_order(
        factory, user_id="u1", ticker="AA", side="yes", action="buy",
        qty=5, type_="limit", limit_price_cents=55, mode="paper",
        orderbook=None,
    )
    cancel = engine.cancel_order(factory, user_id="u1", mode="paper",
                                 order_id=res.order_id)
    assert cancel["ok"] is True
    # Idempotent
    cancel2 = engine.cancel_order(factory, user_id="u1", mode="paper",
                                  order_id=res.order_id)
    assert cancel2["ok"] is False


def test_summary_returns_open_orders_and_positions():
    factory, _ = make_in_memory_conn_factory()
    book = _book(yes_asks=[(55, 10)])
    engine.place_order(factory, user_id="u1", ticker="AA", side="yes",
                       action="buy", qty=5, type_="limit",
                       limit_price_cents=55, mode="paper", orderbook=book)
    summary = engine.get_summary(factory, "u1")
    assert summary["user_id"] == "u1"
    assert len(summary["positions"]) == 1
    assert summary["enrolled_for_live"] is False


# ─── Live path with monkeypatched client ──────────────────────────────────────

def test_live_path_blocked_when_not_enrolled():
    factory, _ = make_in_memory_conn_factory()
    res = engine.place_order(
        factory, user_id="u1", ticker="AA", side="yes", action="buy",
        qty=1, limit_price_cents=50, mode="live",
    )
    assert not res.ok
    assert res.code == "not_enrolled"


def test_live_path_calls_kalshi_when_enrolled(monkeypatch):
    import credential_vault as vault
    factory, _ = make_in_memory_conn_factory()
    pem, _ = make_test_rsa_key()
    vault.store_credentials(factory, "u1", "key-abc", pem)

    captured = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def place_order(self, **kw):
            captured.update(kw)
            return 200, {"order": {"order_id": "kalshi-xyz", "status": "resting"}}

        def cancel_order(self, oid):
            captured["cancel_id"] = oid
            return 200, {"ok": True}

    monkeypatch.setattr("trade_engine._ks.KalshiSignedClient", FakeClient)

    res = engine.place_order(
        factory, user_id="u1", ticker="AA-1", side="yes", action="buy",
        qty=2, limit_price_cents=55, mode="live",
    )
    assert res.ok
    assert res.kalshi_order_id == "kalshi-xyz"
    assert captured["ticker"] == "AA-1"
    assert captured["count"] == 2
    assert captured["yes_price_cents"] == 55
    # Live order log row written
    with factory(readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM live_order_log WHERE user_id='u1'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kalshi_order_id"] == "kalshi-xyz"
    # And an audit row
    with factory(readonly=True) as conn:
        a = conn.execute(
            "SELECT action FROM trade_audit WHERE user_id='u1'"
        ).fetchall()
    assert any(r["action"] == "placed_live" for r in a)


def test_live_error_recorded_but_doesnt_raise(monkeypatch):
    import credential_vault as vault
    factory, _ = make_in_memory_conn_factory()
    pem, _ = make_test_rsa_key()
    vault.store_credentials(factory, "u1", "key-abc", pem)

    class FakeErrClient:
        def __init__(self, *a, **kw):
            pass

        def place_order(self, **kw):
            return 400, {"error": "insufficient_balance"}

    monkeypatch.setattr("trade_engine._ks.KalshiSignedClient", FakeErrClient)
    res = engine.place_order(
        factory, user_id="u1", ticker="AA-1", side="yes", action="buy",
        qty=1, limit_price_cents=55, mode="live",
    )
    assert not res.ok
    assert res.code == "kalshi_error"
    assert res.extra and res.extra["http_status"] == 400
    # Live error logged
    with factory(readonly=True) as conn:
        a = conn.execute(
            "SELECT action FROM trade_audit WHERE user_id='u1'"
        ).fetchall()
    assert any(r["action"] == "live_error" for r in a)


# ─── Mode safety ──────────────────────────────────────────────────────────────

def test_unknown_mode_rejected():
    factory, _ = make_in_memory_conn_factory()
    res = engine.place_order(
        factory, user_id="u1", ticker="AA", side="yes", action="buy",
        qty=1, limit_price_cents=50, mode="cyborg",
    )
    assert not res.ok
    assert res.code == "bad_mode"
