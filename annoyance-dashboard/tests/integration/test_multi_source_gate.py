"""Integration tests for the multi-source corroboration gate.

The gate fires from spike_detector._evaluate_entity and calls
db.get_entity_hourly_counts_by_source, so we seed real rows in a
tempfile sqlite db and exercise the full path.

Scenarios exercised (all from the work spec):
  1. Reddit-only spike with z>>5 → gate blocks, no spike row inserted.
  2. Reddit + Bluesky spike → gate passes, spike fires with sources_breakdown.
  3. Warmup-mode entity → gate bypassed, warmup fires on absolute thresholds.
  4. sources_breakdown JSON is actually persisted on the spike row (not lost).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)


def _hour_iso(dt: datetime) -> str:
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


def _entities_json_for(entity: str) -> str:
    """LIKE '%entity%' filter matches the literal string; just embed the
    entity name. Shape matches classifier output."""
    return json.dumps([{"name": entity, "type": "organization", "salience": 1.0}])


def _seed_baseline_history(db_module, entity: str, current_hour: datetime, *, how_hours: int = 20) -> None:
    """Seed a history shape that drives the detector into statistical mode.

    Two requirements the seeded rows must clear simultaneously:

      1. len(history) >= config.MIN_BASELINE_HOURS (default 48)
      2. >=3 baseline rows at the SAME hour-of-week as the current bucket

    Note 1: `db.get_entity_history` applies LIMIT 336 (hours=24*14). Same-
    hour-of-week repeats every 168h, so a dense hour-by-hour seed over 14
    days only yields 1-2 same-HOW matches within the returned window — not
    enough.

    Solution: seed a mix. ~48 clutter rows at current-1h…current-48h (all
    DIFFERENT hours-of-week from current) to satisfy the length floor,
    plus ~4 anchor rows at exactly current − k*168h (same HOW) to give
    baseline_signals >= 3. Keep total rows < 336 so LIMIT never trims
    anchors.

    count=1, avg_annoyance=20 keeps baseline signals tiny so the detector
    flags a current-hour count=50, avg_annoyance=90 as an outlier.
    """
    with db_module.cursor() as cur:
        # 48 clutter rows at different hours-of-week than `current`.
        for hours_back in range(1, 49):
            hour_dt = current_hour - timedelta(hours=hours_back)
            cur.execute(
                "INSERT OR REPLACE INTO entity_counts (entity, entity_type, hour, count, avg_annoyance) "
                "VALUES (?, ?, ?, ?, ?)",
                (entity, "organization", _hour_iso(hour_dt), 1, 20.0),
            )
        # 4 anchor rows at exact same hour-of-week (k*168h back). The counts
        # jitter slightly (1, 2, 1, 2) so MAD > 0 — otherwise the detector
        # bails with z_score=0 and the gate never gets reached. Still tiny
        # compared to the current-hour spike so z stays huge.
        anchor_counts = [1, 2, 1, 2]
        for idx, k in enumerate(range(1, 5)):
            hour_dt = current_hour - timedelta(hours=k * 168)
            cur.execute(
                "INSERT OR REPLACE INTO entity_counts (entity, entity_type, hour, count, avg_annoyance) "
                "VALUES (?, ?, ?, ?, ?)",
                (entity, "organization", _hour_iso(hour_dt), anchor_counts[idx], 20.0),
            )


def _seed_current_hour_entity_count(
    db_module, entity: str, current_hour: datetime, *, count: int, avg_annoyance: float
) -> None:
    """Seed the CURRENT-hour bucket — this is what triggers the spike evaluation."""
    with db_module.cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO entity_counts (entity, entity_type, hour, count, avg_annoyance) "
            "VALUES (?, ?, ?, ?, ?)",
            (entity, "organization", _hour_iso(current_hour), count, avg_annoyance),
        )


def _seed_posts_with_classifications(
    db_module,
    *,
    source: str,
    entity: str,
    n_posts: int,
    hour: datetime,
) -> None:
    """Seed N posts attributed to `source`, each with a classification that
    mentions `entity`. All posts land in the same hour bucket so the
    per-source query counts them.
    """
    now_iso = hour.isoformat()
    with db_module.cursor() as cur:
        for i in range(n_posts):
            post_id = f"{source}:test-{entity}-{i}-{hour.timestamp()}"
            cur.execute(
                """INSERT INTO posts
                   (id, source, source_channel, author, content, posted_at,
                    fetched_at, url, engagement, keyword, classified)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (post_id, source, f"test-{source}", "tester",
                 f"Test post about {entity}", now_iso, now_iso,
                 f"https://example.com/{post_id}", 1, None),
            )
            cur.execute(
                """INSERT INTO classifications
                   (post_id, annoyance_score, sentiment, primary_topic,
                    entities_json, classified_at, model, is_sensitive, sensitive_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)""",
                (post_id, 80.0, "frustrated", "tech-outage",
                 _entities_json_for(entity), now_iso, "test-model"),
            )


class _GateTestBase(unittest.TestCase):
    """Common fixture: each test class gets a fresh tempfile DB.

    We reload the db + spike_detector + config modules after setting
    DB_PATH so they pick up the temp file. The reload pattern matches
    tests/test_newsletter.py in the gateway project.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        # config reads DB_PATH at import time; override env + reload.
        os.environ["ANNOYANCE_DB_PATH"] = cls._tmp.name

        import config
        importlib.reload(config)
        config.DB_PATH = cls._tmp.name

        import db as db_module
        importlib.reload(db_module)
        db_module.DB_PATH = cls._tmp.name
        db_module.init_db()
        cls.db = db_module

        import spike_detector as sd
        importlib.reload(sd)
        cls.spike_detector = sd

    @classmethod
    def tearDownClass(cls):
        try:
            os.unlink(cls._tmp.name)
        except FileNotFoundError:
            pass

    def setUp(self):
        # Wipe spike/post/classification/entity state between tests so one
        # scenario can't leak into the next.
        with self.db.cursor() as cur:
            for t in ("spikes", "classifications", "posts", "entity_counts"):
                cur.execute(f"DELETE FROM {t}")


class TestMultiSourceGate(_GateTestBase):
    """Exercises _apply_multi_source_gate via the full _evaluate_entity path."""

    def test_reddit_only_spike_blocked_by_gate(self):
        """One viral Reddit thread alone should NOT fire a spike under the
        gate — it's the exact false-positive class this gate kills."""
        # Force prod-like gate behaviour even if env var is set otherwise.
        import config
        config.REQUIRE_MULTI_SOURCE = True

        entity = "TestEntity"
        current = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        # Long, stable baseline so MAD path is engaged (not warmup).
        _seed_baseline_history(self.db, entity, current, how_hours=20)
        # Current hour: enormous spike on composite signal — z will be huge.
        _seed_current_hour_entity_count(
            self.db, entity, current, count=50, avg_annoyance=90.0,
        )
        # But only Reddit contributes to this hour's posts.
        _seed_posts_with_classifications(
            self.db, source="reddit", entity=entity, n_posts=10, hour=current,
        )

        fire, info = self.spike_detector._evaluate_entity(entity, _hour_iso(current))
        self.assertFalse(fire, f"expected gate to block; info={info}")
        self.assertEqual(info.get("reason"), "multi_source_gate_failed")
        # Gate should record what it saw.
        self.assertIn("sources_observed", info)
        self.assertEqual(info["sources_observed"].get("reddit", 0), 10)
        # Only one source contributed >=2 posts.
        self.assertEqual(info.get("sources_contributing"), ["reddit"])

    def test_reddit_plus_bluesky_spike_passes_gate(self):
        """Same signal shape, but split across two sources. Gate passes,
        fire=True, and sources_breakdown is populated for both sources."""
        import config
        config.REQUIRE_MULTI_SOURCE = True

        entity = "CorroboratedEntity"
        current = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        _seed_baseline_history(self.db, entity, current, how_hours=20)
        _seed_current_hour_entity_count(
            self.db, entity, current, count=50, avg_annoyance=90.0,
        )
        _seed_posts_with_classifications(
            self.db, source="reddit", entity=entity, n_posts=6, hour=current,
        )
        _seed_posts_with_classifications(
            self.db, source="bluesky", entity=entity, n_posts=4, hour=current,
        )

        fire, info = self.spike_detector._evaluate_entity(entity, _hour_iso(current))
        self.assertTrue(fire, f"expected fire=True; info={info}")
        self.assertEqual(info.get("mode"), "statistical")

        # Both sources appear in the breakdown.
        breakdown = {b["source"]: b["count"] for b in info.get("sources_breakdown", [])}
        self.assertEqual(breakdown.get("reddit"), 6)
        self.assertEqual(breakdown.get("bluesky"), 4)
        self.assertEqual(sorted(info["sources_contributing"]), ["bluesky", "reddit"])

    def test_warmup_entity_bypasses_gate(self):
        """A brand-new entity with no baseline should fire under warmup
        rules even if only one source contributes. Warmup uses stricter
        absolute thresholds so this is still safe."""
        import config
        config.REQUIRE_MULTI_SOURCE = True

        entity = "WarmupEntity"
        current = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        # NO baseline history — just the current hour, above warmup thresholds
        # (count >= WARMUP_MIN_COUNT, avg_annoyance >= WARMUP_MIN_AVG_ANNOYANCE).
        _seed_current_hour_entity_count(
            self.db, entity, current,
            count=config.WARMUP_MIN_COUNT + 2,
            avg_annoyance=config.WARMUP_MIN_AVG_ANNOYANCE + 5,
        )
        # Only Reddit — normally would fail the gate, but warmup bypasses it.
        _seed_posts_with_classifications(
            self.db, source="reddit", entity=entity,
            n_posts=config.WARMUP_MIN_COUNT + 2, hour=current,
        )

        fire, info = self.spike_detector._evaluate_entity(entity, _hour_iso(current))
        self.assertTrue(fire, f"warmup should fire; info={info}")
        self.assertEqual(info.get("mode"), "warmup")
        # sources_breakdown still populated even on warmup fires so the
        # downstream spike row has the same shape as statistical fires.
        self.assertIn("sources_breakdown", info)

    def test_gate_disabled_allows_single_source_fire(self):
        """The gate respects config.REQUIRE_MULTI_SOURCE — tests and ops
        can flip it off to validate the rest of the pipeline."""
        import config
        config.REQUIRE_MULTI_SOURCE = False

        try:
            entity = "GateOffEntity"
            current = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

            _seed_baseline_history(self.db, entity, current, how_hours=20)
            _seed_current_hour_entity_count(
                self.db, entity, current, count=50, avg_annoyance=90.0,
            )
            # Only Reddit.
            _seed_posts_with_classifications(
                self.db, source="reddit", entity=entity, n_posts=10, hour=current,
            )

            fire, info = self.spike_detector._evaluate_entity(entity, _hour_iso(current))
            self.assertTrue(fire, f"gate was disabled, should fire; info={info}")
        finally:
            config.REQUIRE_MULTI_SOURCE = True


class TestSpikeRowPersistsSourcesBreakdown(_GateTestBase):
    """sources_breakdown must make it all the way onto the spike row so
    the UI can render a per-source breakdown later."""

    def test_sources_breakdown_stored_on_spike_row(self):
        import config
        config.REQUIRE_MULTI_SOURCE = True

        entity = "PersistEntity"
        current = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        _seed_baseline_history(self.db, entity, current, how_hours=20)
        _seed_current_hour_entity_count(
            self.db, entity, current, count=50, avg_annoyance=85.0,
        )
        _seed_posts_with_classifications(
            self.db, source="reddit", entity=entity, n_posts=5, hour=current,
        )
        _seed_posts_with_classifications(
            self.db, source="bluesky", entity=entity, n_posts=3, hour=current,
        )

        # Drive the full detect_and_record path. It queries
        # distinct-entities from entity_counts so our seeded entity must
        # have SUM(count) >= SPIKE_MIN_COUNT.
        async def _run():
            return await self.spike_detector.detect_and_record()

        fired = asyncio.get_event_loop().run_until_complete(_run())
        # At least our entity should have fired.
        handles = {f["entity"] for f in fired}
        self.assertIn(entity, handles, f"expected {entity} in fired={handles}")

        # Now read back the spike row and verify sources_breakdown persists.
        spikes = self.db.get_recent_spikes(limit=10)
        target = next((s for s in spikes if s["entity"] == entity), None)
        self.assertIsNotNone(target, "spike row missing")

        breakdown = {b["source"]: b["count"] for b in target["sources_breakdown"]}
        self.assertEqual(breakdown.get("reddit"), 5)
        self.assertEqual(breakdown.get("bluesky"), 3)

        # And sample_excerpts should be cached too (sub-decision B).
        self.assertIsInstance(target["sample_excerpts"], list)
        self.assertTrue(
            len(target["sample_excerpts"]) > 0,
            "expected at least one sample excerpt cached at insertion",
        )


if __name__ == "__main__":
    unittest.main()
