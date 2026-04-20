"""
End-to-end integration: Reddit fetch → post insert → classifier (mocked
Claude) → aggregator → spike detector.

The multi-source gate lives in another file (test_multi_source_gate.py);
this suite validates the happy-path orchestration with a single source.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

import aggregator
import classifier
import config
import db
import spike_detector


pytestmark = pytest.mark.integration


def _hour(offset: int = 0) -> str:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return (now - timedelta(hours=offset)).isoformat()


def _reddit_children(sub: str, posts: list[dict]) -> dict:
    return {
        "data": {
            "children": [
                {"data": {
                    "id": p["id"],
                    "title": p.get("title", ""),
                    "selftext": p.get("selftext", ""),
                    "created_utc": p.get("created_utc", 1_700_000_000),
                    "ups": p.get("ups", 10),
                    "num_comments": p.get("num_comments", 5),
                    "permalink": f"/r/{sub}/comments/{p['id']}",
                    "author": p.get("author", "u"),
                }}
                for p in posts
            ]
        }
    }


async def test_reddit_fetch_then_classifier_writes_classifications(
    fresh_db, mock_anthropic, mock_httpx, monkeypatch,
):
    """RedditSource.fetch → db.insert_post → classify_pending_posts → rows."""
    monkeypatch.setattr(config, "REDDIT_SUBS", ["mildlyinfuriating"])
    monkeypatch.setattr(config, "REDDIT_REQUEST_SPACING_SECONDS", 0)
    mock_httpx.get("https://www.reddit.com/r/mildlyinfuriating/new.json").mock(
        return_value=httpx.Response(200, json=_reddit_children(
            "mildlyinfuriating",
            [{"id": "abc", "title": "United cancelled my flight", "selftext": ""}],
        )),
    )
    from sources.reddit import RedditSource
    posts = await RedditSource().fetch()
    for p in posts:
        db.insert_post(
            id=p["id"], source=p["source"], content=p["content"],
            posted_at=p["posted_at"], source_channel=p.get("source_channel"),
            author=p.get("author"), url=p.get("url"),
            engagement=p.get("engagement", 0), keyword=p.get("keyword"),
        )

    mock_anthropic.push_text("keep")
    mock_anthropic.push_json([{
        "id": "reddit:abc", "annoyance": 82, "sentiment": "angry",
        "primary_topic": "airline", "entities": [
            {"name": "United", "type": "company", "salience": 0.9, "sentiment": "angry"},
        ],
        "is_sensitive": False, "sensitive_reason": None,
    }])
    result = await classifier.classify_pending_posts(limit=10)
    assert result["classified"] == 1


async def test_end_to_end_spike_fires_warmup(fresh_db, mock_anthropic, monkeypatch):
    """Seed enough classified posts → aggregator → spike_detector.detect_and_record
    fires via warmup (short history) with multi-source contribution."""
    monkeypatch.setattr(config, "REQUIRE_MULTI_SOURCE", True)
    current = _hour(0)
    # 10 reddit + 4 bluesky classified posts about Tesla in current hour,
    # high annoyance ⇒ warmup thresholds met (count>=10 and avg>=70)
    for i in range(10):
        db.insert_post(
            id=f"reddit:{i}", source="reddit",
            content=f"Tesla broke again #{i}", posted_at=current,
            source_channel="r/t", engagement=1,
        )
        db.insert_classification(
            post_id=f"reddit:{i}", annoyance_score=85.0, sentiment="angry",
            primary_topic=None,
            entities=[{"name": "Tesla", "type": "company", "salience": 0.9, "sentiment": "angry"}],
            model="v1",
        )
    for i in range(4):
        db.insert_post(
            id=f"bluesky:{i}", source="bluesky",
            content=f"Tesla again #{i}", posted_at=current,
            source_channel="search:tesla", engagement=1,
        )
        db.insert_classification(
            post_id=f"bluesky:{i}", annoyance_score=82.0, sentiment="angry",
            primary_topic=None,
            entities=[{"name": "Tesla", "type": "company", "salience": 0.9, "sentiment": "angry"}],
            model="v1",
        )
    aggregator.rebuild_hour(current)

    mock_anthropic.push_text("Tesla issues reported across the board.")
    fired = await spike_detector.detect_and_record()
    assert len(fired) == 1
    assert fired[0]["entity"] == "Tesla"

    rows = db.get_recent_spikes(limit=1)
    assert rows[0]["entity"] == "Tesla"
    assert rows[0]["count"] == 14
    assert rows[0]["sample_excerpts"]  # excerpts cached
    assert rows[0]["confidence_score"] is not None


async def test_cost_ceiling_halts_full_pipeline(fresh_db, mock_anthropic):
    """When DAILY_COST_CEILING_CENTS is exceeded, classifier halts cleanly
    and posts stay classified=0 for the next tick."""
    for i in range(3):
        db.insert_post(
            id=f"reddit:{i}", source="reddit", content=f"ugh {i}",
            posted_at=_hour(0), source_channel="r/t", engagement=1,
        )
    # Push today over the ceiling
    db.log_claude_usage(
        operation="classify", model="s", input_tokens=1, output_tokens=1,
        estimated_cost_cents=config.DAILY_COST_CEILING_CENTS + 50.0, post_count=1,
    )
    result = await classifier.classify_pending_posts(limit=10)
    assert result.get("error") == "cost_ceiling"
    # Posts still unclassified
    pending = db.get_unclassified_posts(limit=10)
    assert len(pending) == 3
