"""Polymarket fake — covers market fetch + positions.

The real ``PolymarketClient`` is an async wrapper around httpx calls to
Gamma + CLOB. This mock stubs the two methods the gateway actually
exercises: ``get_markets()`` and ``get_positions(address)``.
"""

from __future__ import annotations

from typing import Optional

import pytest


class MockPolymarketClient:
    """Duck-typed replacement for ``backend.markets.polymarket_client.PolymarketClient``."""

    def __init__(self, markets: Optional[list[dict]] = None,
                 positions: Optional[dict[str, list[dict]]] = None):
        self.markets = markets or []
        # Positions keyed on the wallet address that was requested.
        self.positions = positions or {}
        self.calls: list[dict] = []
        self.closed = False

    async def get_markets(self, **kwargs) -> list[dict]:
        self.calls.append({"method": "get_markets", "kwargs": kwargs})
        return list(self.markets)

    async def get_market(self, slug: str) -> Optional[dict]:
        self.calls.append({"method": "get_market", "slug": slug})
        for m in self.markets:
            if m.get("slug") == slug:
                return m
        return None

    async def get_positions(self, address: str, **kwargs) -> list[dict]:
        self.calls.append({"method": "get_positions", "address": address})
        return list(self.positions.get(address, []))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def mock_polymarket(monkeypatch):
    """Install a MockPolymarketClient on server.POLY_CLIENT."""
    mock = MockPolymarketClient()
    try:
        import server
        monkeypatch.setattr(server, "POLY_CLIENT", mock, raising=False)
    except Exception:
        pass
    # Also patch unified_markets' direct constructor paths used by jobs.
    try:
        from backend.markets import polymarket_client as _pc
        monkeypatch.setattr(_pc, "PolymarketClient", lambda *a, **k: mock)
    except Exception:
        pass
    return mock
