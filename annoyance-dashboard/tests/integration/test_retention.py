"""
Integration tests for decision #3: classifications forever, raw content
dropped at 30d. The retention_loop in server.py calls
db.scrub_raw_content_older_than(days=30) every 6h.

Covered here:
  * Posts older than 30d have content + author scrubbed
  * Their classifications are preserved
  * Aggregates computed off classifications.entities_json still work after scrub
  * Spike cards remain readable via sample_excerpts (sub-decision B)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db
import aggregator


pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _insert_post_at(pid: str, days_ago: float, *, content: str, source: str = "reddit", entity: str = "Tesla"):
    ts = (_now() - timedelta(days=days_ago)).isoformat()
    db.insert_post(id=pid, source=source, content=content, posted_at=ts,
                   source_channel=f"{source}:t", author="a", url=None, engagement=1)
    db.insert_classification(
        post_id=pid, annoyance_score=80.0, sentiment="angry",
        primary_topic=None,
        entities=[{"name": entity, "type": "company", "salience": 0.9, "sentiment": "angry"}],
        model="v1",
    )


def test_scrub_preserves_classification(fresh_db):
    _insert_post_at("reddit:old", days_ago=45, content="ancient complaint about Tesla")
    _insert_post_at("reddit:new", days_ago=1, content="fresh complaint about Tesla")
    scrubbed = db.scrub_raw_content_older_than(days=30)
    assert scrubbed == 1
    with db.cursor() as cur:
        old = cur.execute(
            "SELECT content, author, content_dropped_at FROM posts WHERE id='reddit:old'"
        ).fetchone()
        new = cur.execute("SELECT content FROM posts WHERE id='reddit:new'").fetchone()
        classifications = cur.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
    assert old["content"] == ""
    assert old["author"] is None
    assert old["content_dropped_at"] is not None
    assert "fresh" in new["content"]
    assert classifications == 2


def test_aggregator_still_works_after_scrub(fresh_db):
    """aggregator pulls entities_json from classifications — scrubbed content
    shouldn't matter."""
    old_hour = (_now() - timedelta(days=45)).replace(minute=0, second=0, microsecond=0).isoformat()
    _insert_post_at("reddit:old", days_ago=45, content="ancient complaint about Tesla")
    db.scrub_raw_content_older_than(days=30)
    # Rebuild that historical hour — classification row still joins cleanly
    result = aggregator.rebuild_hour(old_hour)
    assert result["posts"] == 1
    assert result["entities"] == 1


def test_spike_card_readable_after_scrub(fresh_db):
    """sample_excerpts_json cached on the spike row survives raw-content scrub."""
    old_hour = (_now() - timedelta(days=45)).replace(minute=0, second=0, microsecond=0).isoformat()
    sid = db.insert_spike(
        entity="Tesla", detected_hour=old_hour, z_score=4.0,
        multiple_of_baseline=5.0, avg_annoyance=85.0, count=10,
        sample_post_ids=["reddit:ancient_1", "reddit:ancient_2"],
        sample_excerpts=["Tesla autopilot failed today", "Tesla charging broke"],
        confidence_score=72.0,
        sources_breakdown=[{"source": "reddit", "count": 8}, {"source": "bluesky", "count": 3}],
    )
    # Simulate those posts being retention-scrubbed
    for pid in ("reddit:ancient_1", "reddit:ancient_2"):
        db.insert_post(
            id=pid, source="reddit",
            content="Tesla broke my car in unspecified ways",
            posted_at=(_now() - timedelta(days=45)).isoformat(),
            source_channel="r/t", engagement=1,
        )
    db.scrub_raw_content_older_than(days=30)

    # Spike row intact, excerpts intact
    rows = db.get_recent_spikes(limit=1)
    assert rows[0]["id"] == sid
    assert rows[0]["sample_excerpts"] == [
        "Tesla autopilot failed today", "Tesla charging broke",
    ]
    # Original posts exist but their content is gone
    with db.cursor() as cur:
        content = cur.execute(
            "SELECT content FROM posts WHERE id='reddit:ancient_1'"
        ).fetchone()["content"]
    assert content == ""


def test_scrub_ignores_fresh_posts(fresh_db):
    _insert_post_at("reddit:r1", days_ago=5, content="recent")
    _insert_post_at("reddit:r2", days_ago=29, content="still within window")
    assert db.scrub_raw_content_older_than(days=30) == 0


def test_scrub_retention_trigger_via_admin(fresh_db, monkeypatch):
    """Hit /admin/trigger?loop=retention and verify it returns the scrubbed
    count. This exercises the admin integration, not just db.* directly."""
    from fastapi.testclient import TestClient
    import server
    async def _noop(): return
    for name in ("reddit_loop", "bluesky_loop", "classifier_loop",
                 "aggregator_loop", "spike_detector_loop", "retention_loop"):
        monkeypatch.setattr(server, name, _noop)
    import auth
    monkeypatch.setattr(auth, "_client_host", lambda r: "127.0.0.1")

    _insert_post_at("reddit:old", days_ago=45, content="ancient")
    with TestClient(server.app) as client:
        r = client.post("/admin/trigger?loop=retention")
    assert r.status_code == 200
    body = r.json()
    assert body["loop"] == "retention"
    assert body["scrubbed"] == 1
    assert body["ttl_days"] == 30
