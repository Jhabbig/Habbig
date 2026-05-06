"""Tests for the new Manifold + Metaculus aggregators and Polymarket
event-cache reuse.

Run:
    cd backend && python3 test_aggregators.py
"""
from __future__ import annotations

import asyncio
import sys

from aggregators.manifold import ManifoldAggregator
from aggregators.metaculus import MetaculusAggregator
from aggregators.polymarket import PolymarketAggregator


def _expect(actual, expected, label):
    if actual != expected:
        print(f"FAIL {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"PASS {label}")


def test_manifold_binary_outcomes():
    raw = {"outcomeType": "BINARY", "probability": 0.6}
    out = ManifoldAggregator._outcomes(raw)
    if len(out) != 2:
        print("FAIL manifold_binary length")
        sys.exit(1)
    if abs(out[0]["probability"] - 0.6) > 1e-9 or abs(out[1]["probability"] - 0.4) > 1e-9:
        print(f"FAIL manifold_binary probs: {out}")
        sys.exit(1)
    print("PASS manifold_binary")


def test_manifold_multiple_choice():
    raw = {
        "outcomeType": "MULTIPLE_CHOICE",
        "answers": [
            {"text": "Dem", "probability": 0.55, "id": "1"},
            {"text": "Rep", "probability": 0.45, "id": "2"},
        ],
    }
    out = ManifoldAggregator._outcomes(raw)
    if [o["name"] for o in out] != ["Dem", "Rep"]:
        print(f"FAIL manifold_mc names: {out}")
        sys.exit(1)
    print("PASS manifold_multiple_choice")


def test_metaculus_binary():
    raw = {"type": "forecast", "community_prediction": {"full": {"q2": 0.42}}}
    out = MetaculusAggregator._outcomes(raw)
    if abs(out[0]["probability"] - 0.42) > 1e-9 or abs(out[1]["probability"] - 0.58) > 1e-9:
        print(f"FAIL metaculus_binary: {out}")
        sys.exit(1)
    print("PASS metaculus_binary")


def test_metaculus_missing_community_prediction():
    raw = {"type": "forecast", "community_prediction": {}}
    out = MetaculusAggregator._outcomes(raw)
    if out != []:
        print(f"FAIL metaculus_missing_cp: expected [], got {out}")
        sys.exit(1)
    print("PASS metaculus_missing_community_prediction")


def test_polymarket_event_cache_dedupes():
    """fetch_election_markets and fetch_world_election_markets should share a
    single fetch within the cache TTL, not paginate the politics catalog
    twice. This is a regression test for the pre-rewrite double-fetch bug.
    """
    import aiohttp

    agg = PolymarketAggregator()
    http_calls = {"n": 0}

    # Stub aiohttp.ClientSession.get to return one page of fake events the
    # first time and an empty page the second (so the inner pagination loop
    # exits cleanly).
    pages = [
        [
            {
                "title": "2026 US Senate control",
                "slug": "us-senate-2026",
                "tags": [{"slug": "elections"}, {"slug": "senate"}],
                "markets": [{
                    "id": "abc",
                    "question": "Will Democrats win the Senate in 2026?",
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": '["0.46","0.54"]',
                    "clobTokenIds": '["t1","t2"]',
                    "active": True,
                    "closed": False,
                    "endDate": "2027-01-03T00:00:00Z",
                    "volume": 1000.0,
                    "liquidity": 500.0,
                }],
            }
        ],
        [],
    ]

    class _FakeResp:
        def __init__(self, payload):
            self.status = 200
            self._payload = payload
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def json(self): return self._payload

    class _FakeSession:
        closed = False
        def get(self, url, params=None, timeout=None):
            http_calls["n"] += 1
            payload = pages.pop(0) if pages else []
            return _FakeResp(payload)

    agg._session = _FakeSession()
    agg._owns_session = False

    async def run():
        a = await agg.fetch_election_markets()
        b = await agg.fetch_world_election_markets()
        return a, b

    a, b = asyncio.run(run())

    # Within the 4-minute cache TTL, the second call must reuse the cached
    # events and make no additional HTTP requests. The first call paginates
    # until an empty page (or no markets remain) — with our fake that's two
    # GETs total.
    if http_calls["n"] > 2:
        print(f"FAIL polymarket_event_cache_dedupes: expected ≤2 GETs, got {http_calls['n']}")
        sys.exit(1)
    if not a:
        print("FAIL polymarket_event_cache_dedupes: US fetch yielded no markets")
        sys.exit(1)
    print(f"PASS polymarket_event_cache_dedupes (HTTP GETs: {http_calls['n']})")


if __name__ == "__main__":
    test_manifold_binary_outcomes()
    test_manifold_multiple_choice()
    test_metaculus_binary()
    test_metaculus_missing_community_prediction()
    test_polymarket_event_cache_dedupes()
    print("\nAll aggregator tests passed.")
