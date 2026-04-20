"""
Unit tests for spike_detector.py.

Covers:
  * Warmup threshold gates (count>=10 AND avg_annoyance>=70)
  * Statistical three-gate (z>=3, mult>=3, count>=5)
  * Multi-source corroboration gate (config.REQUIRE_MULTI_SOURCE)
  * Sample excerpts cached at insertion (sub-decision B)
  * Confidence score computed and stored
  * Dedup on (entity, detected_hour)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
import db
import spike_detector


def _hour(offset: int = 0) -> str:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return (now - timedelta(hours=offset)).isoformat()


def _seed_entity_hours(entity: str, hours_back: int, count: int, avg: float, current: bool = True) -> None:
    """Backfill entity_counts history for an entity with a given baseline."""
    for h in range(hours_back, 0, -1):
        db.upsert_entity_count(entity, "company", _hour(h), count=count, avg_annoyance=avg)
    if current:
        db.upsert_entity_count(entity, "company", _hour(0), count=count, avg_annoyance=avg)


def _seed_classified_post(pid: str, posted_at: str, entity: str, source: str = "reddit", score: float = 80.0):
    db.insert_post(
        id=pid, source=source, content=f"ugh {entity} is broken",
        posted_at=posted_at, source_channel=f"{source}:test", engagement=1,
    )
    db.insert_classification(
        post_id=pid, annoyance_score=score, sentiment="angry",
        primary_topic=None,
        entities=[{"name": entity, "type": "company", "salience": 0.9, "sentiment": "angry"}],
        model="v1",
    )


# ── _evaluate_entity (pure) ──────────────────────────────────────────────────

def test_evaluate_no_history_returns_false(fresh_db):
    fire, info = spike_detector._evaluate_entity("Ghost", _hour(0))
    assert fire is False
    assert info["reason"] == "no_history"


def test_evaluate_warmup_fires_on_high_absolute(fresh_db, monkeypatch):
    """Entity with < MIN_BASELINE_HOURS history can fire via warmup path."""
    # Ensure multi-source gate doesn't short-circuit warmup (it bypasses by design)
    monkeypatch.setattr(config, "REQUIRE_MULTI_SOURCE", True)
    # 5 hours of history, then a current row that exceeds warmup thresholds.
    for h in range(5, 0, -1):
        db.upsert_entity_count("NewCo", "company", _hour(h), count=2, avg_annoyance=40.0)
    db.upsert_entity_count("NewCo", "company", _hour(0), count=15, avg_annoyance=80.0)
    fire, info = spike_detector._evaluate_entity("NewCo", _hour(0))
    assert fire is True
    assert info["mode"] == "warmup"


def test_evaluate_warmup_rejects_low_volume(fresh_db):
    for h in range(5, -1, -1):
        db.upsert_entity_count("NewCo", "company", _hour(h), count=3, avg_annoyance=50.0)
    fire, info = spike_detector._evaluate_entity("NewCo", _hour(0))
    assert fire is False
    assert info["reason"] == "warmup_threshold_not_met"


def _seed_statistical_baseline(entity: str, *, current_count: int, current_avg: float) -> None:
    """Build enough history to pass MIN_BASELINE_HOURS + >=3 same-hour-of-week
    baseline points with non-uniform counts so MAD > 0 (avoids the z=0 branch).
    """
    same_how_seed = [(168, 1, 30.0), (336, 2, 35.0), (504, 3, 40.0)]
    for h, cnt, avg in same_how_seed:
        db.upsert_entity_count(entity, "company", _hour(h), count=cnt, avg_annoyance=avg)
    # Pad length
    for h in (4, 6, 8, 10, 20, 48, 72, 96, 120, 200, 400):
        db.upsert_entity_count(entity, "company", _hour(h), count=1, avg_annoyance=30.0)
    db.upsert_entity_count(
        entity, "company", _hour(0),
        count=current_count, avg_annoyance=current_avg,
    )


def test_evaluate_statistical_requires_all_three_gates(fresh_db, monkeypatch):
    """z >= 3 AND multiple >= 3 AND count >= 5; all must pass."""
    monkeypatch.setattr(config, "REQUIRE_MULTI_SOURCE", False)
    monkeypatch.setattr(config, "MIN_BASELINE_HOURS", 10)
    _seed_statistical_baseline("Tesla", current_count=20, current_avg=90.0)
    fire, info = spike_detector._evaluate_entity("Tesla", _hour(0))
    assert fire is True
    assert info["mode"] == "statistical"
    assert info["z_score"] >= config.SPIKE_Z_THRESHOLD
    assert info["multiple_of_baseline"] >= config.SPIKE_MULTIPLE_THRESHOLD
    assert info["count"] >= config.SPIKE_MIN_COUNT


def test_evaluate_statistical_rejects_below_count_gate(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_MULTI_SOURCE", False)
    monkeypatch.setattr(config, "MIN_BASELINE_HOURS", 10)
    _seed_statistical_baseline("X", current_count=3, current_avg=95.0)  # count < 5
    fire, info = spike_detector._evaluate_entity("X", _hour(0))
    assert fire is False
    assert info["reason"] == "gates_not_met"


def test_multi_source_gate_blocks_single_source(fresh_db, monkeypatch):
    """Single-source (all reddit) spike fails the corroboration gate."""
    monkeypatch.setattr(config, "REQUIRE_MULTI_SOURCE", True)
    monkeypatch.setattr(config, "MIN_BASELINE_HOURS", 10)
    _seed_statistical_baseline("Apple", current_count=20, current_avg=90.0)
    # All 8 classified posts this hour are reddit
    for i in range(8):
        _seed_classified_post(f"reddit:{i}", _hour(0), "Apple", source="reddit")
    fire, info = spike_detector._evaluate_entity("Apple", _hour(0))
    assert fire is False
    assert info["reason"] == "multi_source_gate_failed"


def test_multi_source_gate_passes_when_two_sources_contribute(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_MULTI_SOURCE", True)
    monkeypatch.setattr(config, "MIN_BASELINE_HOURS", 10)
    _seed_statistical_baseline("Apple", current_count=20, current_avg=90.0)
    for i in range(2):
        _seed_classified_post(f"reddit:{i}", _hour(0), "Apple", source="reddit")
        _seed_classified_post(f"bluesky:{i}", _hour(0), "Apple", source="bluesky")
    fire, info = spike_detector._evaluate_entity("Apple", _hour(0))
    assert fire is True
    assert info["mode"] == "statistical"
    assert len(info["sources_breakdown"]) == 2


# ── detect_and_record (end-to-end) ───────────────────────────────────────────

async def test_detect_and_record_caches_excerpts(fresh_db, mock_anthropic, monkeypatch):
    """Fired spike must have sample_excerpts populated (sub-decision B)."""
    monkeypatch.setattr(config, "REQUIRE_MULTI_SOURCE", True)
    # Push: Haiku summary call returns a sentence.
    mock_anthropic.push_text("United flights cancelled across the board.")
    # Build warmup-fireable state
    for h in range(5, 0, -1):
        db.upsert_entity_count("United Airlines", "company", _hour(h), count=2, avg_annoyance=50.0)
    db.upsert_entity_count("United Airlines", "company", _hour(0), count=15, avg_annoyance=80.0)
    # Classify enough posts in current hour — multi-source
    for i in range(3):
        _seed_classified_post(f"reddit:{i}", _hour(0), "United Airlines", source="reddit",
                              score=85.0)
        _seed_classified_post(f"bluesky:{i}", _hour(0), "United Airlines", source="bluesky",
                              score=75.0)
    fired = await spike_detector.detect_and_record()
    assert len(fired) == 1
    rows = db.get_recent_spikes(limit=1)
    assert rows[0]["entity"] == "United Airlines"
    assert rows[0]["sample_excerpts"]  # non-empty
    assert rows[0]["confidence_score"] is not None


async def test_detect_and_record_is_idempotent(fresh_db, mock_anthropic, monkeypatch):
    """Running detect_and_record twice in the same hour must not duplicate the spike."""
    monkeypatch.setattr(config, "REQUIRE_MULTI_SOURCE", True)
    mock_anthropic.push_text("summary a")
    mock_anthropic.push_text("summary b")  # for second run attempt (unused)
    for h in range(5, 0, -1):
        db.upsert_entity_count("Tesla", "company", _hour(h), count=2, avg_annoyance=50.0)
    db.upsert_entity_count("Tesla", "company", _hour(0), count=15, avg_annoyance=80.0)
    for i in range(3):
        _seed_classified_post(f"r{i}", _hour(0), "Tesla", source="reddit", score=85.0)
        _seed_classified_post(f"b{i}", _hour(0), "Tesla", source="bluesky", score=75.0)
    fired1 = await spike_detector.detect_and_record()
    fired2 = await spike_detector.detect_and_record()
    assert len(fired1) == 1
    # Second call re-evaluates but UNIQUE(entity, detected_hour) prevents a 2nd row;
    # detect_and_record only appends to `fired` if inserted is not None.
    with db.cursor() as cur:
        row = cur.execute("SELECT COUNT(*) FROM spikes WHERE entity='Tesla'").fetchone()
    assert row[0] == 1
    # The second run returns no fired entries (dedup)
    assert fired2 == []


# ── _compute_confidence ──────────────────────────────────────────────────────

def test_compute_confidence_warmup_returns_flat():
    assert spike_detector._compute_confidence(z=0.0, multiple=0.0, warmup=True) == 30.0


def test_compute_confidence_saturates_at_100():
    c = spike_detector._compute_confidence(z=100.0, multiple=100.0, backtest_hit_rate=1.0)
    assert c == 100.0


def test_compute_confidence_low_z_keeps_room():
    """z=3 is the gate edge → z component is 0; multiple=3 → 0; backtest=0 → 0."""
    c = spike_detector._compute_confidence(z=3.0, multiple=3.0, backtest_hit_rate=0.0)
    assert c == 0.0
