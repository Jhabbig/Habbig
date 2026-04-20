"""
Unit tests for classifier.py.

Every test mocks the Anthropic client via the `mock_anthropic` fixture — no
real API traffic. Classifier internals we lock in:
  * Haiku triage keep/skip gate, fail-open on parse failure
  * Cost ceiling halts before Haiku AND between Haiku and Sonnet
  * Sonnet JSON parse → retry → poison pipeline
  * Entity hallucination gate (content substring check)
  * is_sensitive + sensitive_reason round-trip
  * Triage-skip posts recorded as classified with triage_score marker
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import classifier
import config
import db


# ── Helpers ──────────────────────────────────────────────────────────────────

def _seed_posts(n: int, *, content: str = "annoying post"):
    """Seed N posts as unclassified."""
    for i in range(n):
        db.insert_post(
            id=f"reddit:{i}",
            source="reddit",
            content=f"{content} {i}",
            posted_at=db.now_iso(),
            source_channel="reddit:test",
            author="a",
            url=None,
            engagement=1,
        )


def _triage_text(decisions: list[str]) -> str:
    """Helper: build a triage response body like 'keep\nkeep\nskip'."""
    return "\n".join(decisions)


# ── Triage pass ──────────────────────────────────────────────────────────────

async def test_triage_keep_short_circuits_skip(fresh_db, mock_anthropic):
    _seed_posts(2)
    mock_anthropic.push_text(_triage_text(["keep", "keep"]))  # triage
    mock_anthropic.push_json([
        {"id": "reddit:0", "annoyance": 70, "sentiment": "frustrated",
         "primary_topic": "x", "entities": [], "is_sensitive": False,
         "sensitive_reason": None},
        {"id": "reddit:1", "annoyance": 40, "sentiment": "neutral",
         "primary_topic": "y", "entities": [], "is_sensitive": False,
         "sensitive_reason": None},
    ])  # classify
    result = await classifier.classify_pending_posts(limit=10)
    assert result == {"triaged": 2, "classified": 2, "skipped": 0}


async def test_triage_skip_short_circuits_no_sonnet_call(fresh_db, mock_anthropic):
    """If Haiku says skip for all posts, Sonnet is never called."""
    _seed_posts(3)
    mock_anthropic.push_text(_triage_text(["skip", "skip", "skip"]))
    result = await classifier.classify_pending_posts(limit=10)
    assert result == {"triaged": 3, "classified": 0, "skipped": 3}
    # Only one call (triage), no Sonnet call
    assert len(mock_anthropic.calls) == 1


async def test_triage_skip_records_classification_marker(fresh_db, mock_anthropic):
    """Skipped posts still get a classification row (zero score) so they don't
    keep queueing every tick."""
    _seed_posts(1)
    mock_anthropic.push_text("skip")
    await classifier.classify_pending_posts(limit=10)
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT annoyance_score, model, triage_score FROM classifications WHERE post_id=?",
            ("reddit:0",),
        ).fetchone()
    assert row["annoyance_score"] == 0.0
    assert "triage" in row["model"].lower()
    assert row["triage_score"] == 0.0


async def test_triage_parse_failure_falls_back_to_all_keep(fresh_db, mock_anthropic):
    """Gibberish output from Haiku → treat all as keep so we don't drop signal."""
    _seed_posts(2)
    mock_anthropic.push_text("???\n!!!!")  # unparseable as keep/skip
    # Sonnet classify still happens
    mock_anthropic.push_json([
        {"id": "reddit:0", "annoyance": 70, "sentiment": "angry",
         "primary_topic": None, "entities": [], "is_sensitive": False,
         "sensitive_reason": None},
        {"id": "reddit:1", "annoyance": 60, "sentiment": "frustrated",
         "primary_topic": None, "entities": [], "is_sensitive": False,
         "sensitive_reason": None},
    ])
    result = await classifier.classify_pending_posts(limit=10)
    assert result["classified"] == 2


async def test_triage_network_failure_falls_back_to_all_keep(fresh_db, mock_anthropic):
    _seed_posts(1)
    mock_anthropic.push_raise(RuntimeError("simulated 500"))
    mock_anthropic.push_json([
        {"id": "reddit:0", "annoyance": 50, "sentiment": "neutral",
         "primary_topic": None, "entities": [], "is_sensitive": False,
         "sensitive_reason": None},
    ])
    result = await classifier.classify_pending_posts(limit=10)
    assert result["classified"] == 1


# ── Sonnet classify pass ─────────────────────────────────────────────────────

async def test_sonnet_captures_is_sensitive(fresh_db, mock_anthropic):
    _seed_posts(1)
    mock_anthropic.push_text("keep")
    mock_anthropic.push_json([{
        "id": "reddit:0", "annoyance": 80, "sentiment": "angry",
        "primary_topic": "violence", "entities": [],
        "is_sensitive": True, "sensitive_reason": "violence",
    }])
    await classifier.classify_pending_posts(limit=10)
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT is_sensitive, sensitive_reason FROM classifications WHERE post_id=?",
            ("reddit:0",),
        ).fetchone()
    assert row["is_sensitive"] == 1
    assert row["sensitive_reason"] == "violence"


async def test_sonnet_sanitizes_invalid_sensitive_reason(fresh_db, mock_anthropic):
    _seed_posts(1)
    mock_anthropic.push_text("keep")
    mock_anthropic.push_json([{
        "id": "reddit:0", "annoyance": 80, "sentiment": "angry",
        "primary_topic": None, "entities": [],
        "is_sensitive": True, "sensitive_reason": "made-up-category",
    }])
    await classifier.classify_pending_posts(limit=10)
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT sensitive_reason FROM classifications WHERE post_id=?",
            ("reddit:0",),
        ).fetchone()
    assert row["sensitive_reason"] == "other"


async def test_sonnet_drops_hallucinated_entities(fresh_db, mock_anthropic):
    """Entity that doesn't appear in the post content gets dropped."""
    db.insert_post(
        id="reddit:0", source="reddit",
        content="My flight got cancelled.",
        posted_at=db.now_iso(), source_channel="r/x", engagement=1,
    )
    mock_anthropic.push_text("keep")
    mock_anthropic.push_json([{
        "id": "reddit:0", "annoyance": 80, "sentiment": "angry",
        "primary_topic": None,
        "entities": [
            {"name": "United Airlines", "type": "company", "salience": 0.9, "sentiment": "angry"},
            {"name": "Cruise Line Z", "type": "company", "salience": 0.8, "sentiment": "angry"},
        ],
        "is_sensitive": False, "sensitive_reason": None,
    }])
    await classifier.classify_pending_posts(limit=10)
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT entities_json FROM classifications WHERE post_id=?",
            ("reddit:0",),
        ).fetchone()
    # "United Airlines" isn't in the content either — both get dropped
    parsed = json.loads(row["entities_json"])
    assert parsed == []


async def test_sonnet_keeps_entity_when_content_mentions_it(fresh_db, mock_anthropic):
    db.insert_post(
        id="reddit:0", source="reddit",
        content="United Airlines cancelled my flight again.",
        posted_at=db.now_iso(), source_channel="r/x", engagement=1,
    )
    mock_anthropic.push_text("keep")
    mock_anthropic.push_json([{
        "id": "reddit:0", "annoyance": 80, "sentiment": "angry",
        "primary_topic": None,
        "entities": [
            {"name": "United Airlines", "type": "company", "salience": 0.9, "sentiment": "angry"},
        ],
        "is_sensitive": False, "sensitive_reason": None,
    }])
    await classifier.classify_pending_posts(limit=10)
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT entities_json FROM classifications WHERE post_id=?",
            ("reddit:0",),
        ).fetchone()
    parsed = json.loads(row["entities_json"])
    assert len(parsed) == 1
    assert parsed[0]["name"] == "United Airlines"


async def test_sonnet_parse_failure_retries_then_poisons(fresh_db, mock_anthropic):
    """Two bad Sonnet responses → batch marked classified=2 (poisoned), not infinite loop."""
    _seed_posts(1)
    mock_anthropic.push_text("keep")
    mock_anthropic.push_text("this is not JSON")
    mock_anthropic.push_text("still not JSON")
    result = await classifier.classify_pending_posts(limit=10)
    assert result["classified"] == 0
    with db.cursor() as cur:
        row = cur.execute("SELECT classified FROM posts WHERE id=?", ("reddit:0",)).fetchone()
    assert row["classified"] == 2  # poisoned


async def test_sonnet_matches_by_id_not_order(fresh_db, mock_anthropic):
    """Response can reorder items; classifier matches via the id field."""
    _seed_posts(2)
    mock_anthropic.push_text(_triage_text(["keep", "keep"]))
    # Sonnet returns posts in reversed order
    mock_anthropic.push_json([
        {"id": "reddit:1", "annoyance": 30, "sentiment": "neutral",
         "primary_topic": None, "entities": [], "is_sensitive": False, "sensitive_reason": None},
        {"id": "reddit:0", "annoyance": 90, "sentiment": "angry",
         "primary_topic": None, "entities": [], "is_sensitive": False, "sensitive_reason": None},
    ])
    await classifier.classify_pending_posts(limit=10)
    with db.cursor() as cur:
        by_id = {
            r["post_id"]: r["annoyance_score"]
            for r in cur.execute("SELECT post_id, annoyance_score FROM classifications").fetchall()
        }
    assert by_id["reddit:0"] == 90.0
    assert by_id["reddit:1"] == 30.0


async def test_sonnet_missing_post_rolls_over(fresh_db, mock_anthropic):
    """Sonnet drops a post from its response → that post stays classified=0."""
    _seed_posts(2)
    mock_anthropic.push_text(_triage_text(["keep", "keep"]))
    mock_anthropic.push_json([{
        "id": "reddit:0", "annoyance": 80, "sentiment": "angry",
        "primary_topic": None, "entities": [],
        "is_sensitive": False, "sensitive_reason": None,
    }])  # only returns one post
    result = await classifier.classify_pending_posts(limit=10)
    assert result["classified"] == 1
    # Missing post still unclassified
    with db.cursor() as cur:
        row = cur.execute("SELECT classified FROM posts WHERE id=?", ("reddit:1",)).fetchone()
    assert row["classified"] == 0


async def test_sonnet_clamps_annoyance_range(fresh_db, mock_anthropic):
    _seed_posts(1)
    mock_anthropic.push_text("keep")
    mock_anthropic.push_json([{
        "id": "reddit:0", "annoyance": 250, "sentiment": "angry",
        "primary_topic": None, "entities": [],
        "is_sensitive": False, "sensitive_reason": None,
    }])
    await classifier.classify_pending_posts(limit=10)
    with db.cursor() as cur:
        score = cur.execute(
            "SELECT annoyance_score FROM classifications WHERE post_id=?",
            ("reddit:0",),
        ).fetchone()["annoyance_score"]
    assert score == 100.0


# ── Cost ceiling enforcement ─────────────────────────────────────────────────

async def test_cost_ceiling_halts_before_triage(fresh_db, mock_anthropic, monkeypatch):
    """Daily cost >= ceiling → skip entire batch, return error sentinel."""
    _seed_posts(5)
    # Fake usage spike that exceeds ceiling
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    db.log_claude_usage(
        operation="classify", model="s", input_tokens=1, output_tokens=1,
        estimated_cost_cents=config.DAILY_COST_CEILING_CENTS + 10.0,
        post_count=1,
    )
    result = await classifier.classify_pending_posts(limit=10)
    assert result == {"triaged": 0, "classified": 0, "skipped": 0, "error": "cost_ceiling"}
    assert mock_anthropic.calls == []  # no API call issued


async def test_cost_ceiling_halts_between_triage_and_sonnet(fresh_db, mock_anthropic, monkeypatch):
    """Ceiling check runs before Sonnet too — so a big Haiku call can stop us."""
    _seed_posts(2)
    mock_anthropic.push_text(_triage_text(["keep", "keep"]),
                             input_tokens=10, output_tokens=5)

    real_log = db.log_claude_usage

    def _inflate(**kwargs):
        # Simulate triage itself tipping us past the ceiling
        kwargs["estimated_cost_cents"] = config.DAILY_COST_CEILING_CENTS + 1.0
        return real_log(**kwargs)

    monkeypatch.setattr(db, "log_claude_usage", _inflate)
    result = await classifier.classify_pending_posts(limit=10)
    assert result["error"] == "cost_ceiling"
    # Triage ran (one call), Sonnet did NOT
    assert len(mock_anthropic.calls) == 1


# ── Spike summary (Haiku per decision #12) ────────────────────────────────────

async def test_summarize_spike_returns_stripped_single_line(fresh_db, mock_anthropic):
    mock_anthropic.push_text('  "Flights cancelled with no notice."  ')
    summary = await classifier.summarize_spike(
        "United Airlines",
        [{"content": "United cancelled my flight."}],
    )
    assert summary == "Flights cancelled with no notice."


async def test_summarize_spike_returns_none_on_failure(fresh_db, mock_anthropic):
    mock_anthropic.push_raise(RuntimeError("offline"))
    assert await classifier.summarize_spike("X", [{"content": "bad"}]) is None


async def test_summarize_spike_no_samples_returns_none(fresh_db, mock_anthropic):
    assert await classifier.summarize_spike("X", []) is None


# ── Cost logging ─────────────────────────────────────────────────────────────

async def test_triage_records_claude_usage(fresh_db, mock_anthropic):
    _seed_posts(1)
    mock_anthropic.push_text("keep", input_tokens=120, output_tokens=4)
    mock_anthropic.push_json([{
        "id": "reddit:0", "annoyance": 40, "sentiment": "neutral",
        "primary_topic": None, "entities": [],
        "is_sensitive": False, "sensitive_reason": None,
    }], input_tokens=500, output_tokens=80)
    await classifier.classify_pending_posts(limit=10)
    # Two rows — triage + classify
    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT operation, input_tokens, output_tokens FROM claude_usage ORDER BY id"
        ).fetchall()
    ops = [r["operation"] for r in rows]
    assert "triage" in ops
    assert "classify" in ops
