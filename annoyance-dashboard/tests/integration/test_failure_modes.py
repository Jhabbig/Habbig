"""
Failure-injection integration suite. Verifies every documented failure path
short-circuits cleanly without corrupting state or crashing the loop.

Scenarios:
  1. Reddit 429   → per-sub backoff, other subs still fetched
  2. Reddit 500   → logged, other subs still fetched
  3. Bluesky 429  → per-term backoff
  4. Claude 5xx   → triage falls back to all-keep; Sonnet call fails → 0 classified
  5. Claude bad JSON → retry; still bad → batch marked poisoned (classified=2)
  6. Cost ceiling mid-batch → remaining posts roll over unclassified
  7. DB unique conflict on spike → insert_spike returns None (dedup)
  8. Clock drift (post posted_at in future) → insert + classify still work
  9. Malformed Reddit response (missing keys) → post skipped, no crash
 10. Empty Anthropic response → no classifications, no crash
 11. Sonnet response with length mismatch → match by id, surplus dropped
 12. Classification on scrubbed post → succeeds (content already empty)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

import classifier
import config
import db
import spike_detector


pytestmark = pytest.mark.integration


def _now_plus(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# ── 1. Reddit 429 per-sub backoff ────────────────────────────────────────────

async def test_reddit_429_per_sub_backoff_other_subs_still_work(mock_httpx, monkeypatch):
    from sources.reddit import RedditSource
    from sources import reddit as reddit_mod
    monkeypatch.setattr(config, "REDDIT_SUBS", ["bad", "good"])
    monkeypatch.setattr(config, "REDDIT_REQUEST_SPACING_SECONDS", 0)
    mock_httpx.get("https://www.reddit.com/r/bad/new.json").mock(
        return_value=httpx.Response(429),
    )
    mock_httpx.get("https://www.reddit.com/r/good/new.json").mock(
        return_value=httpx.Response(200, json={"data": {"children": [
            {"data": {"id": "g1", "title": "ok", "created_utc": 1700000000,
                      "ups": 1, "num_comments": 0, "permalink": "/r/good/1"}},
        ]}}),
    )
    posts = await RedditSource().fetch()
    assert "bad" in reddit_mod._backoff
    assert any(p["id"] == "reddit:g1" for p in posts)


async def test_reddit_500_continues_other_subs(mock_httpx, monkeypatch):
    from sources.reddit import RedditSource
    monkeypatch.setattr(config, "REDDIT_SUBS", ["err", "ok"])
    monkeypatch.setattr(config, "REDDIT_REQUEST_SPACING_SECONDS", 0)
    mock_httpx.get("https://www.reddit.com/r/err/new.json").mock(
        return_value=httpx.Response(500),
    )
    mock_httpx.get("https://www.reddit.com/r/ok/new.json").mock(
        return_value=httpx.Response(200, json={"data": {"children": [
            {"data": {"id": "o1", "title": "complaint", "created_utc": 1700000000,
                      "ups": 1, "num_comments": 0, "permalink": "/r/ok/1"}},
        ]}}),
    )
    posts = await RedditSource().fetch()
    assert any(p["id"] == "reddit:o1" for p in posts)


# ── 3. Bluesky 429 per-term backoff ──────────────────────────────────────────

async def test_bluesky_429_term_backoff(mock_httpx, monkeypatch):
    from sources.bluesky import BlueskySource
    from sources import bluesky as bsky_mod
    monkeypatch.setattr(config, "BLUESKY_SEARCH_TERMS", ["hot"])
    monkeypatch.setattr(config, "BLUESKY_REQUEST_SPACING_SECONDS", 0)
    mock_httpx.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(429),
    )
    await BlueskySource().fetch()
    assert "hot" in bsky_mod._backoff


# ── 4-5. Claude failures ─────────────────────────────────────────────────────

async def test_claude_5xx_during_triage_falls_back_then_sonnet_poisons(fresh_db, mock_anthropic):
    for i in range(2):
        db.insert_post(
            id=f"reddit:{i}", source="reddit", content="ugh",
            posted_at=db.now_iso(), source_channel="r/t", engagement=1,
        )
    # Haiku raises → forward to Sonnet as keep. Sonnet then gets empty canned
    # responses (no push_json queued) → parse fails on first and retry → poisoned.
    mock_anthropic.push_raise(RuntimeError("Anthropic 529 overloaded"))
    result = await classifier.classify_pending_posts(limit=10)
    assert result["classified"] == 0
    # Posts have been marked poisoned (classified=2) so they won't infinite-loop.
    with db.cursor() as cur:
        rows = cur.execute("SELECT classified FROM posts").fetchall()
    assert all(r[0] == 2 for r in rows)


async def test_claude_bad_json_retries_then_poisons(fresh_db, mock_anthropic):
    db.insert_post(id="reddit:x", source="reddit", content="ugh",
                   posted_at=db.now_iso(), source_channel="r/t", engagement=1)
    mock_anthropic.push_text("keep")
    mock_anthropic.push_text("<<< invalid >>>")
    mock_anthropic.push_text("still bad")
    await classifier.classify_pending_posts(limit=10)
    with db.cursor() as cur:
        status = cur.execute("SELECT classified FROM posts WHERE id='reddit:x'").fetchone()[0]
    assert status == 2  # poisoned — no infinite loop


# ── 6. Cost ceiling mid-batch ────────────────────────────────────────────────

async def test_cost_ceiling_mid_batch_rolls_over(fresh_db, mock_anthropic, monkeypatch):
    """Ceiling hit after triage → remaining posts stay unclassified."""
    for i in range(3):
        db.insert_post(id=f"reddit:{i}", source="reddit", content=f"ugh {i}",
                       posted_at=db.now_iso(), source_channel="r/t", engagement=1)
    mock_anthropic.push_text("keep\nkeep\nkeep")
    real_log = db.log_claude_usage

    def _inflate(**kw):
        kw["estimated_cost_cents"] = config.DAILY_COST_CEILING_CENTS + 1.0
        return real_log(**kw)

    monkeypatch.setattr(db, "log_claude_usage", _inflate)
    result = await classifier.classify_pending_posts(limit=10)
    assert result.get("error") == "cost_ceiling"
    # Posts unclassified still
    assert len(db.get_unclassified_posts(limit=10)) == 3


# ── 7. DB unique conflict on spike dedup ─────────────────────────────────────

def test_spike_dedup_returns_none_on_duplicate(fresh_db):
    h = db.current_hour_iso()
    first = db.insert_spike(
        entity="Tesla", detected_hour=h, z_score=4.0,
        multiple_of_baseline=5.0, avg_annoyance=80.0, count=10,
        sample_post_ids=[],
    )
    second = db.insert_spike(
        entity="Tesla", detected_hour=h, z_score=9.0,
        multiple_of_baseline=9.0, avg_annoyance=99.0, count=99,
        sample_post_ids=[],
    )
    assert first is not None
    assert second is None


# ── 8. Clock drift ───────────────────────────────────────────────────────────

def test_post_with_future_timestamp_still_insertable(fresh_db):
    """A clock-skewed source may provide posted_at in the future. Must not crash."""
    fut = _now_plus(3600)  # 1h ahead
    ok = db.insert_post(
        id="reddit:fut", source="reddit", content="future complaint",
        posted_at=fut, source_channel="r/t", engagement=1,
    )
    assert ok is True
    assert db.get_unclassified_posts(limit=10)[0]["id"] == "reddit:fut"


# ── 9. Malformed Reddit response ─────────────────────────────────────────────

async def test_malformed_reddit_response_doesnt_crash(mock_httpx, monkeypatch):
    """Child without 'data' key, or with missing title/id, must not crash the fetch."""
    from sources.reddit import RedditSource
    monkeypatch.setattr(config, "REDDIT_SUBS", ["weird"])
    monkeypatch.setattr(config, "REDDIT_REQUEST_SPACING_SECONDS", 0)
    mock_httpx.get("https://www.reddit.com/r/weird/new.json").mock(
        return_value=httpx.Response(200, json={"data": {"children": [
            {},                                        # empty
            {"data": {"title": "no id"}},              # missing id
            {"data": {"id": "g", "title": "ok",
                      "created_utc": 1700000000, "ups": 1, "num_comments": 0,
                      "permalink": "/r/weird/g"}},
        ]}}),
    )
    posts = await RedditSource().fetch()
    ids = [p["id"] for p in posts]
    assert ids == ["reddit:g"]  # only the well-formed one


# ── 10. Empty Claude response ────────────────────────────────────────────────

async def test_empty_claude_response_classifies_nothing(fresh_db, mock_anthropic):
    db.insert_post(id="reddit:e", source="reddit", content="x",
                   posted_at=db.now_iso(), source_channel="r/t", engagement=1)
    mock_anthropic.push_text("keep")
    mock_anthropic.push_text("")  # empty Sonnet body
    mock_anthropic.push_text("")  # empty retry
    await classifier.classify_pending_posts(limit=10)
    with db.cursor() as cur:
        status = cur.execute("SELECT classified FROM posts WHERE id='reddit:e'").fetchone()[0]
    assert status == 2  # poisoned (empty also fails parse)


# ── 11. Sonnet length mismatch ───────────────────────────────────────────────

async def test_sonnet_drops_surplus_ids_not_in_request(fresh_db, mock_anthropic):
    db.insert_post(id="reddit:a", source="reddit", content="ugh A",
                   posted_at=db.now_iso(), source_channel="r/t", engagement=1)
    mock_anthropic.push_text("keep")
    # Response contains both the right id AND a made-up one → by-id match drops surplus
    mock_anthropic.push_json([
        {"id": "reddit:a", "annoyance": 60, "sentiment": "frustrated",
         "primary_topic": None, "entities": [], "is_sensitive": False, "sensitive_reason": None},
        {"id": "reddit:phantom", "annoyance": 99, "sentiment": "angry",
         "primary_topic": None, "entities": [], "is_sensitive": False, "sensitive_reason": None},
    ])
    result = await classifier.classify_pending_posts(limit=10)
    assert result["classified"] == 1
    with db.cursor() as cur:
        count = cur.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
    assert count == 1


# ── 12. Classification on a scrubbed post ────────────────────────────────────

def test_classification_survives_content_scrub(fresh_db):
    """A post whose content was scrubbed still has its classification row;
    attempting to re-insert a classification doesn't break anything."""
    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    db.insert_post(id="reddit:o", source="reddit", content="original",
                   posted_at=old_ts, source_channel="r/t", engagement=1)
    db.insert_classification(
        post_id="reddit:o", annoyance_score=70.0, sentiment="angry",
        primary_topic=None, entities=[], model="v1",
    )
    db.scrub_raw_content_older_than(days=30)
    # Upsert a new classification — model column should reflect v2
    db.insert_classification(
        post_id="reddit:o", annoyance_score=80.0, sentiment="angry",
        primary_topic="updated", entities=[], model="v2",
    )
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT model, primary_topic FROM classifications WHERE post_id='reddit:o'"
        ).fetchone()
    assert row["model"] == "v2"
    assert row["primary_topic"] == "updated"


# ── 13. SMTP failure must not block spike detector ──────────────────────────

async def test_missing_notifications_module_does_not_break_spike(
    fresh_db, mock_anthropic, monkeypatch,
):
    """Notifications module may fail to import — spike detector must still record."""
    monkeypatch.setattr(config, "REQUIRE_MULTI_SOURCE", True)
    current = db.current_hour_iso()
    for i in range(10):
        db.insert_post(id=f"reddit:{i}", source="reddit",
                       content=f"Tesla issue {i}", posted_at=current,
                       source_channel="r/t", engagement=1)
        db.insert_classification(
            post_id=f"reddit:{i}", annoyance_score=80.0, sentiment="angry",
            primary_topic=None,
            entities=[{"name": "Tesla", "type": "company", "salience": 0.9}],
            model="v1",
        )
    for i in range(3):
        db.insert_post(id=f"bluesky:{i}", source="bluesky",
                       content=f"Tesla issue {i}", posted_at=current,
                       source_channel="search:tesla", engagement=1)
        db.insert_classification(
            post_id=f"bluesky:{i}", annoyance_score=80.0, sentiment="angry",
            primary_topic=None,
            entities=[{"name": "Tesla", "type": "company", "salience": 0.9}],
            model="v1",
        )
    import aggregator
    aggregator.rebuild_hour(current)
    mock_anthropic.push_text("Tesla having a bad week")
    fired = await spike_detector.detect_and_record()
    # Even though the notifications import fails (no module), the spike must fire.
    assert len(fired) == 1
    assert db.get_recent_spikes(limit=1)[0]["entity"] == "Tesla"
