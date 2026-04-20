"""
Unit tests for db.py. Verifies schema + helper behaviour without touching
the network or Claude.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import db


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hour_iso(offset_hours: int = 0) -> str:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return (now - timedelta(hours=offset_hours)).isoformat()


def _make_post(pid: str, *, posted_at: str = None, content: str = "ugh", source: str = "reddit") -> None:
    db.insert_post(
        id=pid,
        source=source,
        content=content,
        posted_at=posted_at or db.now_iso(),
        source_channel=f"{source}:test",
        author="a",
        url=None,
        engagement=1,
    )


# ── Schema / init ────────────────────────────────────────────────────────────

def test_init_db_is_idempotent(fresh_db):
    """Calling init_db twice is safe — the _COLUMN_MIGRATIONS loop must not
    double-add columns."""
    db.init_db()
    db.init_db()
    # Schema intact — insert round-trips a post
    _make_post("reddit:a1")
    with db.cursor() as cur:
        row = cur.execute("SELECT id FROM posts WHERE id = ?", ("reddit:a1",)).fetchone()
    assert row is not None
    assert row["id"] == "reddit:a1"


def test_posts_have_migration_columns(fresh_db):
    """content_dropped_at and the other migrated columns must exist."""
    with db.cursor() as cur:
        cols = {r[1] for r in cur.execute("PRAGMA table_info(posts)").fetchall()}
        class_cols = {r[1] for r in cur.execute("PRAGMA table_info(classifications)").fetchall()}
        spike_cols = {r[1] for r in cur.execute("PRAGMA table_info(spikes)").fetchall()}
    assert "content_dropped_at" in cols
    assert {"is_sensitive", "sensitive_reason", "triage_score"}.issubset(class_cols)
    assert {"sample_excerpts_json", "confidence_score", "sources_json"}.issubset(spike_cols)


# ── Posts ────────────────────────────────────────────────────────────────────

def test_insert_post_new_returns_true(fresh_db):
    assert db.insert_post(
        id="reddit:1", source="reddit", content="frustrated",
        posted_at=_hour_iso(0),
    ) is True


def test_insert_post_duplicate_returns_false(fresh_db):
    db.insert_post(id="reddit:1", source="reddit", content="frustrated", posted_at=_hour_iso(0))
    assert db.insert_post(id="reddit:1", source="reddit", content="dup", posted_at=_hour_iso(0)) is False


def test_get_unclassified_orders_by_posted_at_desc(fresh_db):
    _make_post("reddit:old", posted_at=_hour_iso(5))
    _make_post("reddit:new", posted_at=_hour_iso(0))
    _make_post("reddit:mid", posted_at=_hour_iso(2))
    rows = db.get_unclassified_posts(limit=10)
    ids = [r["id"] for r in rows]
    assert ids == ["reddit:new", "reddit:mid", "reddit:old"]


def test_mark_classified_updates_status(fresh_db):
    _make_post("reddit:1")
    db.mark_classified("reddit:1", status=1)
    assert db.get_unclassified_posts(limit=10) == []


def test_mark_many_classified_batch(fresh_db):
    _make_post("reddit:1")
    _make_post("reddit:2")
    db.mark_many_classified(["reddit:1", "reddit:2"], status=2)
    assert db.get_unclassified_posts(limit=10) == []


# ── Classifications ──────────────────────────────────────────────────────────

def test_insert_classification_stores_sensitive_and_triage(fresh_db):
    _make_post("reddit:1")
    db.insert_classification(
        post_id="reddit:1",
        annoyance_score=72.5,
        sentiment="angry",
        primary_topic="outage",
        entities=[{"name": "Tesla", "type": "company", "salience": 0.9, "sentiment": "angry"}],
        model="claude-sonnet-4-5+classifyv1",
        is_sensitive=True,
        sensitive_reason="harassment",
        triage_score=0.88,
    )
    with db.cursor() as cur:
        row = cur.execute(
            """SELECT annoyance_score, sentiment, primary_topic, entities_json,
                      is_sensitive, sensitive_reason, triage_score
               FROM classifications WHERE post_id=?""",
            ("reddit:1",),
        ).fetchone()
    assert row["annoyance_score"] == 72.5
    assert row["sentiment"] == "angry"
    assert row["primary_topic"] == "outage"
    assert row["is_sensitive"] == 1
    assert row["sensitive_reason"] == "harassment"
    assert row["triage_score"] == 0.88
    parsed = json.loads(row["entities_json"])
    assert parsed[0]["name"] == "Tesla"


def test_insert_classification_replaces_on_duplicate(fresh_db):
    """INSERT OR REPLACE — rerunning the classifier overwrites, not appends."""
    _make_post("reddit:1")
    db.insert_classification(
        post_id="reddit:1", annoyance_score=10.0, sentiment="neutral",
        primary_topic=None, entities=[], model="v1",
    )
    db.insert_classification(
        post_id="reddit:1", annoyance_score=90.0, sentiment="angry",
        primary_topic="later", entities=[], model="v2",
    )
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT annoyance_score, model FROM classifications WHERE post_id=?",
            ("reddit:1",),
        ).fetchone()
    assert row["annoyance_score"] == 90.0
    assert row["model"] == "v2"


def test_get_classifications_in_hour_filters_by_window(fresh_db):
    hour = _hour_iso(0)
    _make_post("reddit:inside", posted_at=hour)
    _make_post("reddit:outside", posted_at=_hour_iso(5))
    for pid in ("reddit:inside", "reddit:outside"):
        db.insert_classification(
            post_id=pid, annoyance_score=70.0, sentiment="angry",
            primary_topic=None, entities=[], model="v1",
        )
    rows = db.get_classifications_in_hour(hour)
    ids = [r["post_id"] for r in rows]
    assert "reddit:inside" in ids
    assert "reddit:outside" not in ids


# ── Aggregate index + entity counts ──────────────────────────────────────────

def test_upsert_annoyance_index_overwrites(fresh_db):
    hour = _hour_iso(0)
    db.upsert_annoyance_index(hour, score=30.0, post_count=5, sources={"reddit": {"count": 5, "avg_score": 30.0}})
    db.upsert_annoyance_index(hour, score=55.0, post_count=10, sources={"reddit": {"count": 10, "avg_score": 55.0}})
    data = db.get_annoyance_index(hours=24)
    assert len(data) == 1
    assert data[0]["score"] == 55.0
    assert data[0]["post_count"] == 10


def test_upsert_entity_count_dedup(fresh_db):
    hour = _hour_iso(0)
    db.upsert_entity_count("Tesla", "company", hour, count=3, avg_annoyance=60.0)
    db.upsert_entity_count("Tesla", "company", hour, count=9, avg_annoyance=82.0)
    rows = db.get_entity_history("Tesla", hours=24)
    assert len(rows) == 1
    assert rows[0]["count"] == 9
    assert rows[0]["avg_annoyance"] == 82.0


def test_get_top_entities_ranks_by_composite(fresh_db):
    hour = _hour_iso(0)
    db.upsert_entity_count("Low", "company", hour, count=10, avg_annoyance=20.0)   # signal 4
    db.upsert_entity_count("High", "company", hour, count=5, avg_annoyance=90.0)   # signal 9
    db.upsert_entity_count("Mid", "company", hour, count=6, avg_annoyance=50.0)    # signal 6
    rows = db.get_top_entities_for_hour(hour, limit=5)
    assert [r["entity"] for r in rows] == ["High", "Mid", "Low"]


def test_get_latest_hour_with_entity_data_prefers_most_recent(fresh_db):
    db.upsert_entity_count("A", "company", _hour_iso(5), count=3, avg_annoyance=50.0)
    db.upsert_entity_count("A", "company", _hour_iso(0), count=4, avg_annoyance=60.0)
    assert db.get_latest_hour_with_entity_data() == _hour_iso(0)


def test_get_distinct_entities_respects_min_count(fresh_db):
    hour = _hour_iso(0)
    db.upsert_entity_count("Solo", "company", hour, count=2, avg_annoyance=40.0)
    db.upsert_entity_count("Many", "company", hour, count=12, avg_annoyance=80.0)
    out = db.get_distinct_entities_with_min_count(5)
    assert out == ["Many"]


# ── Spikes ───────────────────────────────────────────────────────────────────

def test_insert_spike_dedup_on_entity_hour(fresh_db):
    h = _hour_iso(0)
    first = db.insert_spike(
        entity="Tesla", detected_hour=h, z_score=4.0, multiple_of_baseline=5.0,
        avg_annoyance=80.0, count=10, sample_post_ids=[], summary="s",
    )
    second = db.insert_spike(
        entity="Tesla", detected_hour=h, z_score=9.9, multiple_of_baseline=9.9,
        avg_annoyance=99.0, count=99, sample_post_ids=[], summary="dup",
    )
    assert first is not None
    assert second is None


def test_insert_spike_caches_sample_excerpts_and_confidence(fresh_db):
    h = _hour_iso(0)
    excerpts = ["United just cancelled my flight.", "UA lost my bag again.", "United is the worst."]
    sid = db.insert_spike(
        entity="United Airlines", detected_hour=h, z_score=4.5, multiple_of_baseline=5.5,
        avg_annoyance=85.0, count=12, sample_post_ids=["reddit:1", "reddit:2"],
        summary="Cancellations across multiple posts.",
        sample_excerpts=excerpts, confidence_score=78.5,
        sources_breakdown=[{"source": "reddit", "count": 8}, {"source": "bluesky", "count": 4}],
    )
    assert sid is not None
    rows = db.get_recent_spikes(limit=1)
    assert rows[0]["sample_excerpts"] == excerpts
    assert rows[0]["confidence_score"] == 78.5
    assert rows[0]["sources_breakdown"] == [{"source": "reddit", "count": 8}, {"source": "bluesky", "count": 4}]


def test_insert_spike_without_excerpts_defaults_empty_list(fresh_db):
    h = _hour_iso(0)
    db.insert_spike(
        entity="X", detected_hour=h, z_score=4.0, multiple_of_baseline=4.0,
        avg_annoyance=70.0, count=8, sample_post_ids=[],
    )
    rows = db.get_recent_spikes(limit=1)
    assert rows[0]["sample_excerpts"] == []
    assert rows[0]["sources_breakdown"] == []


def test_get_posts_by_ids_returns_in_any_order(fresh_db):
    _make_post("reddit:1")
    _make_post("reddit:2")
    rows = db.get_posts_by_ids(["reddit:2", "reddit:1"])
    assert sorted(r["id"] for r in rows) == ["reddit:1", "reddit:2"]


def test_get_posts_by_ids_empty_returns_empty(fresh_db):
    assert db.get_posts_by_ids([]) == []


# ── Multi-source helper ──────────────────────────────────────────────────────

def test_entity_hourly_counts_by_source_groups_by_source(fresh_db):
    hour = _hour_iso(0)
    # two reddit posts, one bluesky post, all mention Apple
    _make_post("reddit:1", posted_at=hour, source="reddit")
    _make_post("reddit:2", posted_at=hour, source="reddit")
    _make_post("bluesky:1", posted_at=hour, source="bluesky")
    for pid in ("reddit:1", "reddit:2", "bluesky:1"):
        db.insert_classification(
            post_id=pid, annoyance_score=70.0, sentiment="angry",
            primary_topic=None,
            entities=[{"name": "Apple", "type": "company", "salience": 0.9, "sentiment": "angry"}],
            model="v1",
        )
    counts = db.get_entity_hourly_counts_by_source("Apple", hour)
    assert counts == {"reddit": 2, "bluesky": 1}


def test_entity_hourly_source_stats_counts_distinct_authors(fresh_db):
    """The enriched helper returns posts AND unique authors per source.
    A source where 3 posts come from 1 author must report unique_authors=1
    so the admin FP queue can flag it as suspicious (P4.1)."""
    hour = _hour_iso(0)
    # reddit: 3 posts from ONE author (looks like gaming)
    for i, author in enumerate(["spammer", "spammer", "spammer"]):
        db.insert_post(
            id=f"reddit:{i}", source="reddit", content="ugh Apple",
            posted_at=hour, source_channel="reddit:test", author=author,
        )
    # bluesky: 2 posts from TWO distinct authors (looks organic)
    for i, author in enumerate(["alice", "bob"]):
        db.insert_post(
            id=f"bluesky:{i}", source="bluesky", content="ugh Apple",
            posted_at=hour, source_channel="bluesky:test", author=author,
        )
    for pid in ("reddit:0", "reddit:1", "reddit:2", "bluesky:0", "bluesky:1"):
        db.insert_classification(
            post_id=pid, annoyance_score=70.0, sentiment="angry",
            primary_topic=None,
            entities=[{"name": "Apple", "type": "company", "salience": 0.9, "sentiment": "angry"}],
            model="v1",
        )
    stats = db.get_entity_hourly_source_stats("Apple", hour)
    assert stats == {
        "reddit":  {"posts": 3, "unique_authors": 1},
        "bluesky": {"posts": 2, "unique_authors": 2},
    }


def test_entity_hourly_source_stats_anonymous_posts_count_separately(fresh_db):
    """Empty / NULL author means the post is attributed to nobody in
    particular — each should count as its own distinct "author" so the
    ratio can't be gamed by spamming with a null author field."""
    hour = _hour_iso(0)
    for i in range(3):
        db.insert_post(
            id=f"reddit:anon-{i}", source="reddit", content="ugh Apple",
            posted_at=hour, source_channel="reddit:test", author=None,
        )
    for pid in ("reddit:anon-0", "reddit:anon-1", "reddit:anon-2"):
        db.insert_classification(
            post_id=pid, annoyance_score=70.0, sentiment="angry",
            primary_topic=None,
            entities=[{"name": "Apple", "type": "company", "salience": 0.9}],
            model="v1",
        )
    stats = db.get_entity_hourly_source_stats("Apple", hour)
    assert stats["reddit"]["posts"] == 3
    # Each NULL-author post counts as its own pseudo-author keyed on id —
    # otherwise a bad actor could collapse their spam into "one author".
    assert stats["reddit"]["unique_authors"] == 3


# ── Claude usage / cost ──────────────────────────────────────────────────────

def test_cost_cents_since_sums_only_recent(fresh_db):
    # Two old rows, two recent
    t_old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    t_new = db.now_iso()
    for _ in range(2):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO claude_usage(timestamp, operation, model, input_tokens, output_tokens, estimated_cost_cents, post_count) VALUES (?, 'triage', 'h', 100, 50, 0.5, 1)",
                (t_old,),
            )
    for _ in range(2):
        db.log_claude_usage(
            operation="classify", model="s", input_tokens=100, output_tokens=50,
            estimated_cost_cents=1.25, post_count=1,
        )
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).isoformat()
    assert db.cost_cents_since(today_start) == pytest.approx(2.5)


# ── FP flag ──────────────────────────────────────────────────────────────────

def test_fp_flag_insert_and_resolve(fresh_db):
    h = _hour_iso(0)
    sid = db.insert_spike(
        entity="X", detected_hour=h, z_score=4.0, multiple_of_baseline=4.0,
        avg_annoyance=70.0, count=8, sample_post_ids=[],
    )
    flag_id = db.insert_fp_flag(spike_id=sid, user_id="42", user_email="u@x", reason="duplicate")
    queue = db.list_fp_queue(resolved=False)
    assert len(queue) == 1
    assert queue[0]["id"] == flag_id
    assert db.resolve_fp_flag(flag_id, note="merged") is True
    assert db.list_fp_queue(resolved=False) == []
    assert len(db.list_fp_queue(resolved=True)) == 1


# ── Retention TTL ────────────────────────────────────────────────────────────

def test_scrub_raw_content_older_than_drops_content_not_row(fresh_db):
    """30d retention: content zeroed, row + classification survive."""
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=45)).isoformat()
    new_ts = now.isoformat()
    _make_post("reddit:old", posted_at=old_ts, content="ancient private content")
    _make_post("reddit:new", posted_at=new_ts, content="still fresh")
    # Classify both to prove classification survives scrub
    for pid in ("reddit:old", "reddit:new"):
        db.insert_classification(
            post_id=pid, annoyance_score=50.0, sentiment="frustrated",
            primary_topic=None, entities=[], model="v1",
        )

    scrubbed = db.scrub_raw_content_older_than(days=30)
    assert scrubbed == 1

    with db.cursor() as cur:
        old = cur.execute(
            "SELECT content, author, content_dropped_at FROM posts WHERE id=?",
            ("reddit:old",),
        ).fetchone()
        new = cur.execute(
            "SELECT content FROM posts WHERE id=?", ("reddit:new",),
        ).fetchone()
        class_rows = cur.execute("SELECT COUNT(*) FROM classifications").fetchone()
    assert old["content"] == ""
    assert old["author"] is None
    assert old["content_dropped_at"] is not None
    assert new["content"] == "still fresh"
    assert class_rows[0] == 2  # both classifications intact


def test_scrub_is_idempotent(fresh_db):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    _make_post("reddit:old", posted_at=old_ts, content="ancient")
    assert db.scrub_raw_content_older_than(days=30) == 1
    # Second run shouldn't touch already-scrubbed rows
    assert db.scrub_raw_content_older_than(days=30) == 0


# ── Helpers ──────────────────────────────────────────────────────────────────

def test_bucket_hour_rounds_down(fresh_db):
    assert db.bucket_hour("2026-04-20T12:34:56+00:00").startswith("2026-04-20T12:00:00")


def test_current_hour_iso_has_zero_minutes(fresh_db):
    h = db.current_hour_iso()
    parsed = datetime.fromisoformat(h)
    assert parsed.minute == 0 and parsed.second == 0
