"""Kalshi fake — login, balance, positions, place_order.

Matches the async surface of ``backend.markets.kalshi_client.KalshiClient``
without opening a network connection. Tests set the `behaviour` dict to
control what each method returns or whether it surfaces
``error="token_expired"`` to exercise the 401 → deactivate path.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest


class MockKalshiClient:
    def __init__(self, *, base_url: str = "https://kalshi.test",
                 markets: Optional[list[dict]] = None):
        self.base_url = base_url
        self.markets = markets or []
        self.login_responses: list[dict] = []
        self.balance_response: dict[str, Any] = {"balance": 100_00}
        self.position_response: dict[str, Any] = {"positions": []}
        self.order_response: dict[str, Any] = {"order_id": "ord_test", "status": "submitted"}
        self.calls: list[dict] = []
        self.closed = False

    # ── Markets ─────────────────────────────────────────────────
    async def get_markets(self, **kwargs) -> list[dict]:
        self.calls.append({"method": "get_markets", "kwargs": kwargs})
        return list(self.markets)

    async def get_market(self, ticker: str) -> Optional[dict]:
        self.calls.append({"method": "get_market", "ticker": ticker})
        for m in self.markets:
            if m.get("ticker") == ticker:
                return m
        return None

    # ── Auth ────────────────────────────────────────────────────
    async def login(self, email: str, password: str) -> dict:
        self.calls.append({"method": "login", "email": email})
        if self.login_responses:
            return self.login_responses.pop(0)
        return {
            "token": "kalshi-mock-token",
            "member_id": f"m-{email.split('@', 1)[0]}",
        }

    async def get_balance(self, token: str) -> dict:
        self.calls.append({"method": "get_balance", "token": token})
        return dict(self.balance_response)

    async def get_positions(self, token: str) -> dict:
        self.calls.append({"method": "get_positions", "token": token})
        return dict(self.position_response)

    async def place_order(self, token: str, *, ticker: str, side: str,
                          order_type: str, count: int,
                          price: Optional[int] = None) -> dict:
        self.calls.append({
            "method": "place_order", "token": token,
            "ticker": ticker, "side": side, "type": order_type,
            "count": count, "price": price,
        })
        return dict(self.order_response)

    # ── Lifecycle ───────────────────────────────────────────────
    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def mock_kalshi(monkeypatch):
    mock = MockKalshiClient()
    try:
        import server
        monkeypatch.setattr(server, "KALSHI_CLIENT", mock, raising=False)
    except Exception:
        pass
    try:
        from backend.markets import kalshi_client as _kc
        monkeypatch.setattr(_kc, "KalshiClient", lambda *a, **k: mock)
    except Exception:
        pass
    return mock
