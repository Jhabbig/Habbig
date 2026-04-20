"""Integration test for the entity drill-in page.

Seeds one spike + a couple of recent classifications for a known entity,
then asserts all four of the drill-in API sub-endpoints respond with a
sane shape for a Pro user.

Covers:
  * GET /api/entity/{name}            (history — existing endpoint)
  * GET /api/entity/{name}/spikes     (new)
  * GET /api/entity/{name}/recent-posts (new)
  * GET /api/entity/{name}/markets    (new — pulls from entity_markets.json)

Also spot-checks the paywall: a free-tier client gets 402 from every
entity sub-endpoint.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

import db
from tests.conftest import pro_headers, free_headers


pytestmark = pytest.mark.integration


_ENTITY = "Apple"
_ENCODED = urllib.parse.quote(_ENTITY)


def _seed_entity_data() -> int:
    """Drop one spike + a couple of classifications for _ENTITY. Returns the spike_id."""
    # 1. A handful of hourly counts across 2+ weeks so get_entity_history returns data
    hour = db.current_hour_iso()
    db.upsert_entity_count(_ENTITY, "brand", hour, count=14, avg_annoyance=72.0)

    # 2. A couple of classified posts mentioning the entity (raw posts first)
    db.insert_post(
        id="t:apple-1", source="reddit", source_channel="r/apple",
        content="Apple is being sketchy again about iPhone battery replacements",
        posted_at=hour, url="https://example.test/1",
    )
    db.insert_post(
        id="t:apple-2", source="bluesky", source_channel="bsky",
        content="Third time this week Apple crashed my MacBook, unacceptable",
        posted_at=hour, url="https://example.test/2",
    )
    db.insert_classification(
        post_id="t:apple-1",
        annoyance_score=78.0,
        sentiment="negative",
        primary_topic="product_quality",
        entities=[{"name": _ENTITY, "type": "brand"}],
        model="test-model",
        is_sensitive=False,
    )
    db.insert_classification(
        post_id="t:apple-2",
        annoyance_score=82.0,
        sentiment="negative",
        primary_topic="product_quality",
        entities=[{"name": _ENTITY, "type": "brand"}],
        model="test-model",
        is_sensitive=True,
        sensitive_reason="strong language",
    )

    # 3. A spike firing on this entity, in the same hour
    spike_id = db.insert_spike(
        entity=_ENTITY,
        detected_hour=hour,
        z_score=4.1,
        multiple_of_baseline=3.8,
        avg_annoyance=80.0,
        count=14,
        sample_post_ids=["t:apple-1", "t:apple-2"],
        summary="Users frustrated with iPhone battery + MacBook crashes",
        sample_excerpts=[
            "Apple is being sketchy again about iPhone battery replacements",
            "Third time this week Apple crashed my MacBook",
        ],
        confidence_score=68.4,
        sources_breakdown=[
            {"source": "reddit", "count": 8},
            {"source": "bluesky", "count": 6},
        ],
    )
    assert spike_id is not None, "seeded spike did not insert"
    return spike_id


class TestEntityDrillIn:
    def test_history_endpoint(self, fresh_db, test_client, paywall_env):
        _seed_entity_data()
        r = test_client.get(f"/api/entity/{_ENCODED}", headers=pro_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["entity"] == _ENTITY
        assert isinstance(body["history"], list)
        assert len(body["history"]) >= 1
        row = body["history"][0]
        assert "count" in row and "avg_annoyance" in row

    def test_spikes_endpoint(self, fresh_db, test_client, paywall_env):
        spike_id = _seed_entity_data()
        r = test_client.get(f"/api/entity/{_ENCODED}/spikes", headers=pro_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["entity"] == _ENTITY
        assert isinstance(body["spikes"], list)
        assert len(body["spikes"]) == 1
        s = body["spikes"][0]
        assert s["id"] == spike_id
        assert s["entity"] == _ENTITY
        # JSON columns must be deserialized, not raw strings
        assert isinstance(s["sample_excerpts"], list)
        assert isinstance(s["sources_breakdown"], list)
        assert s["confidence_score"] == pytest.approx(68.4)

    def test_recent_posts_endpoint(self, fresh_db, test_client, paywall_env):
        _seed_entity_data()
        r = test_client.get(f"/api/entity/{_ENCODED}/recent-posts", headers=pro_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["entity"] == _ENTITY
        posts = body["posts"]
        assert len(posts) == 2
        # Sensitivity should propagate — second post was seeded is_sensitive=True
        sensitive = [p for p in posts if p["is_sensitive"]]
        assert len(sensitive) == 1
        assert sensitive[0]["sensitive_reason"] == "strong language"

    def test_markets_endpoint(self, fresh_db, test_client, paywall_env, tmp_path, monkeypatch):
        """Markets endpoint reads entity_markets.json. Override the cache
        path so the test doesn't depend on whatever is checked in."""
        import server
        # URL must be on the allowlist (narve.ai, polymarket.com, kalshi.com)
        # or the loader drops it — see url_guard.
        fake_markets = {_ENTITY: [
            {"title": "Test market", "url": "https://narve.ai/markets/test-1", "source": "placeholder"},
        ]}
        markets_path = tmp_path / "entity_markets.json"
        markets_path.write_text(json.dumps(fake_markets))
        monkeypatch.setattr(server, "_ENTITY_MARKETS_PATH", markets_path)
        monkeypatch.setattr(server, "_ENTITY_MARKETS_CACHE", None)

        r = test_client.get(f"/api/entity/{_ENCODED}/markets", headers=pro_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["entity"] == _ENTITY
        assert body["markets"] == fake_markets[_ENTITY]

    def test_markets_endpoint_unknown_entity(self, fresh_db, test_client, paywall_env):
        r = test_client.get("/api/entity/NoSuchEntity/markets", headers=pro_headers())
        assert r.status_code == 200
        assert r.json() == {"entity": "NoSuchEntity", "markets": []}

    def test_free_tier_blocked(self, fresh_db, test_client, paywall_env):
        _seed_entity_data()
        # Every entity sub-endpoint should 402 for a free-tier client.
        for path in (
            f"/api/entity/{_ENCODED}",
            f"/api/entity/{_ENCODED}/spikes",
            f"/api/entity/{_ENCODED}/recent-posts",
            f"/api/entity/{_ENCODED}/markets",
        ):
            r = test_client.get(path, headers=free_headers())
            assert r.status_code == 402, f"{path} should be paywalled for free tier"
