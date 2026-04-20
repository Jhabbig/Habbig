"""
Unit tests for aggregator.py — entity canonicalization + hour rebuild.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
import aggregator


def _hour(offset: int = 0) -> str:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return (now - timedelta(hours=offset)).isoformat()


def _classify(post_id: str, posted_at: str, score: float, entities: list[dict], source: str = "reddit") -> None:
    db.insert_post(
        id=post_id, source=source, content="x",
        posted_at=posted_at, source_channel=f"{source}:t", author="a",
        url=None, engagement=1,
    )
    db.insert_classification(
        post_id=post_id, annoyance_score=score, sentiment="angry",
        primary_topic=None, entities=entities, model="v1",
    )


# ── canonicalize ─────────────────────────────────────────────────────────────

def test_canonicalize_maps_alias():
    assert aggregator.canonicalize("UAL") == "United Airlines"
    assert aggregator.canonicalize("united airlines") == "United Airlines"
    assert aggregator.canonicalize("@united") == "United Airlines"


def test_canonicalize_case_insensitive():
    assert aggregator.canonicalize("UNITED") == "United Airlines"
    assert aggregator.canonicalize("  United  ") == "United Airlines"


def test_canonicalize_unknown_passes_through():
    assert aggregator.canonicalize("RandomBrand") == "RandomBrand"


def test_canonicalize_strips_leading_at():
    # Explicit @-prefix mappings exist for major entities; verify.
    assert aggregator.canonicalize("@tesla") == "Tesla"


# ── rebuild_hour ─────────────────────────────────────────────────────────────

def test_rebuild_hour_empty_deletes_row(fresh_db):
    """Empty hours must not be written as zeros — they'd fake dips on the chart."""
    h = _hour(0)
    # Pre-seed an index row + entity_counts row, then rebuild the now-empty hour.
    db.upsert_annoyance_index(h, 30.0, 5, {"reddit": {"count": 5, "avg_score": 30.0}})
    db.upsert_entity_count("Stale", "company", h, count=3, avg_annoyance=50.0)
    result = aggregator.rebuild_hour(h)
    assert result == {"hour": h, "posts": 0, "entities": 0, "index": 0.0}
    assert db.get_annoyance_index(hours=24) == []
    assert db.get_entity_history("Stale", hours=24) == []


def test_rebuild_hour_averages_scores(fresh_db):
    h = _hour(0)
    _classify("reddit:1", h, 80.0, [{"name": "Tesla", "type": "company", "salience": 1.0, "sentiment": "angry"}])
    _classify("reddit:2", h, 40.0, [{"name": "Tesla", "type": "company", "salience": 1.0, "sentiment": "angry"}])
    result = aggregator.rebuild_hour(h)
    assert result["posts"] == 2
    assert result["index"] == 60.0


def test_rebuild_hour_groups_by_canonical_entity(fresh_db):
    """Alias collapse: UAL + United Airlines + @united roll up to one row."""
    h = _hour(0)
    _classify("reddit:1", h, 80.0, [{"name": "UAL", "type": "company", "salience": 1.0}])
    _classify("reddit:2", h, 80.0, [{"name": "United Airlines", "type": "company", "salience": 1.0}])
    _classify("reddit:3", h, 80.0, [{"name": "@united", "type": "company", "salience": 1.0}])
    aggregator.rebuild_hour(h)
    rows = db.get_entity_history("United Airlines", hours=24)
    assert len(rows) == 1
    assert rows[0]["count"] == 3
    # Should NOT have fragments:
    for frag in ("UAL", "@united"):
        assert db.get_entity_history(frag, hours=24) == []


def test_rebuild_hour_weights_by_salience(fresh_db):
    """Per-entity avg annoyance should be post_score * salience (with 0.3 floor)."""
    h = _hour(0)
    _classify("reddit:a", h, 100.0, [{"name": "Tesla", "type": "company", "salience": 1.0}])
    _classify("reddit:b", h, 100.0, [{"name": "Tesla", "type": "company", "salience": 0.5}])
    aggregator.rebuild_hour(h)
    rows = db.get_entity_history("Tesla", hours=24)
    assert rows[0]["count"] == 2
    # avg = (100*1.0 + 100*0.5) / 2 = 75
    assert rows[0]["avg_annoyance"] == 75.0


def test_rebuild_hour_applies_salience_floor(fresh_db):
    """Salience 0 must not zero out the weighted score (min 0.3)."""
    h = _hour(0)
    _classify("reddit:a", h, 100.0, [{"name": "Tesla", "type": "company", "salience": 0.0}])
    aggregator.rebuild_hour(h)
    rows = db.get_entity_history("Tesla", hours=24)
    assert rows[0]["count"] == 1
    # Weighted = 100 * max(0.3, 0.0) = 30
    assert rows[0]["avg_annoyance"] == 30.0


def test_rebuild_hour_source_breakdown_in_index(fresh_db):
    """sources_json on annoyance_index records per-source counts."""
    h = _hour(0)
    _classify("reddit:1", h, 80.0, [], source="reddit")
    _classify("reddit:2", h, 60.0, [], source="reddit")
    _classify("bluesky:1", h, 90.0, [], source="bluesky")
    aggregator.rebuild_hour(h)
    data = db.get_annoyance_index(hours=24)
    assert data[0]["sources"]["reddit"]["count"] == 2
    assert data[0]["sources"]["bluesky"]["count"] == 1


def test_rebuild_hour_drops_entities_without_name(fresh_db):
    h = _hour(0)
    _classify("reddit:1", h, 80.0, [{"name": "", "type": "company", "salience": 1.0}])
    aggregator.rebuild_hour(h)
    # The post still contributes to the index
    data = db.get_annoyance_index(hours=24)
    assert data[0]["post_count"] == 1
    # But no entity_counts row from that empty name
    assert data[0]["score"] == 80.0


# ── rebuild_recent ───────────────────────────────────────────────────────────

def test_rebuild_recent_hits_current_and_prev(fresh_db):
    _classify("reddit:cur", _hour(0), 80.0, [{"name": "Tesla", "type": "company", "salience": 1.0}])
    _classify("reddit:prev", _hour(1), 70.0, [{"name": "Tesla", "type": "company", "salience": 1.0}])
    results = aggregator.rebuild_recent()
    # Both hours returned, in order (prev then current)
    assert len(results) == 2
    assert {r["hour"] for r in results} == {_hour(0), _hour(1)}
    # Both classified
    assert all(r["posts"] == 1 for r in results)
