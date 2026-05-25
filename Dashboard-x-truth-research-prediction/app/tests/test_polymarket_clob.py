"""Polymarket CLOB helper tests — no network calls."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.markets.polymarket_clob import (
    OrderBook,
    PolymarketCLOBClient,
    _parse_book_side,
    avg_fill_price,
    slippage_bps,
)


def test_parse_book_dict_shape():
    parsed = _parse_book_side([{"price": "0.40", "size": "100"}, {"price": "0.39", "size": "50"}])
    assert parsed == [(0.40, 100.0), (0.39, 50.0)]


def test_parse_book_list_shape():
    parsed = _parse_book_side([["0.40", "100"], ["0.39", "50"]])
    assert parsed == [(0.40, 100.0), (0.39, 50.0)]


def test_parse_book_skips_zero_and_invalid():
    parsed = _parse_book_side([
        {"price": "0", "size": "100"},        # skip — price 0
        {"price": "0.40", "size": "0"},       # skip — size 0
        {"price": "bad", "size": "10"},       # skip — non-numeric
        {"price": "0.30", "size": "5"},       # keep
    ])
    assert parsed == [(0.30, 5.0)]


def test_avg_fill_price_single_level():
    # $100 stake fills entirely at the best ask of 0.40.
    # 100 / 0.40 = 250 shares, all at 0.40.
    asks = [(0.40, 1000.0)]
    assert abs(avg_fill_price(asks, 100) - 0.40) < 1e-9


def test_avg_fill_price_walks_levels():
    # $100 stake, asks 0.40@$50, 0.50@$200.
    # Level 1: 50 * 0.40 = $20 spent for 50 shares.
    # Level 2: $80 remaining at 0.50 = 160 shares.
    # Total: 210 shares for $100 -> avg = 100/210 ≈ 0.4762.
    asks = [(0.40, 50.0), (0.50, 200.0)]
    fill = avg_fill_price(asks, 100)
    assert abs(fill - (100 / 210)) < 1e-6


def test_avg_fill_price_returns_none_when_book_too_thin():
    # Book has 100 shares at 0.40 = $40 of available liquidity total.
    asks = [(0.40, 100.0)]
    # $40 stake fills exactly — avg price 0.40.
    assert avg_fill_price(asks, 40) == 0.40
    # $100 stake exceeds the $40 of liquidity -> None.
    assert avg_fill_price(asks, 100) is None


def test_slippage_bps_positive():
    # Mid 0.40, fill 0.42 -> +500 bps slippage
    assert slippage_bps(0.40, 0.42) == 500


def test_slippage_bps_handles_none():
    assert slippage_bps(None, 0.42) is None
    assert slippage_bps(0.40, None) is None


def test_orderbook_mid_and_best_bid_ask():
    book = OrderBook(market_token_id="t", bids=[(0.39, 100), (0.38, 200)], asks=[(0.41, 100), (0.42, 50)])
    assert book.best_bid == 0.39
    assert book.best_ask == 0.41
    assert book.mid == 0.40


@pytest.mark.asyncio
async def test_book_for_side_caches_token_ids(monkeypatch):
    """Two calls to ``book_for_side`` should only fetch the gamma-api once."""
    client = PolymarketCLOBClient()

    gamma_calls = 0

    class _Resp:
        def __init__(self, status=200, payload=None):
            self.status_code = status
            self._payload = payload
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("HTTP error")
        def json(self):
            return self._payload

    async def fake_get(self, url, params=None, **kwargs):
        nonlocal gamma_calls
        if "gamma-api" in url:
            gamma_calls += 1
            return _Resp(200, [{"clobTokenIds": '["yes-token", "no-token"]'}])
        # CLOB book
        return _Resp(200, {"bids": [{"price": "0.39", "size": "100"}], "asks": [{"price": "0.41", "size": "100"}]})

    with patch("httpx.AsyncClient.get", new=fake_get):
        b1 = await client.book_for_side("some-slug", "YES")
        b2 = await client.book_for_side("some-slug", "NO")

    assert b1 is not None and b2 is not None
    assert gamma_calls == 1  # second call hit the cache
