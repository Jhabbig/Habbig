"""
Retention-loop unit tests. Verifies the 30d TTL scrubber:

  - Zeroes posts.content and posts.author
  - Stamps content_dropped_at
  - Leaves classifications.entities_json untouched so aggregator still joins
  - Is idempotent (second run scrubs nothing)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.TemporaryDirectory()
import config as _config  # noqa: E402
_config.DB_PATH = Path(_TMP.name) / "retention.db"

import db  # noqa: E402
db._local.__dict__.clear()
db.init_db()


def _backdate(post_id: str, days_ago: int) -> None:
    """Set posted_at to `days_ago` days back — the scrubber cutoff hook."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with db.cursor() as cur:
        cur.execute("UPDATE posts SET posted_at = ? WHERE id = ?", (cutoff, post_id))


class TestRetention(unittest.TestCase):
    def setUp(self):
        # Clean slate
        with db.cursor() as cur:
            cur.execute("DELETE FROM classifications")
            cur.execute("DELETE FROM posts")

    def test_scrub_old_post_clears_content_and_author(self):
        pid = "unit:retention:old"
        db.insert_post(
            id=pid, source="unit",
            content="United Airlines lost my bag again", posted_at="unused",
            author="alice",
        )
        _backdate(pid, 35)
        # Also classify so we can verify the classification survives
        db.insert_classification(
            post_id=pid, annoyance_score=85.0, sentiment="angry",
            primary_topic=None,
            entities=[{"name": "United Airlines", "type": "company",
                       "salience": 0.9, "sentiment": "angry"}],
            model="unit-test",
        )

        scrubbed = db.scrub_raw_content_older_than(days=30)
        self.assertEqual(scrubbed, 1)

        with db.cursor() as cur:
            row = cur.execute(
                "SELECT content, author, content_dropped_at FROM posts WHERE id = ?",
                (pid,),
            ).fetchone()
        self.assertEqual(row["content"], "")
        self.assertIsNone(row["author"])
        self.assertIsNotNone(row["content_dropped_at"])

        # Classification still intact — aggregator/spike_detector depend on this
        with db.cursor() as cur:
            c = cur.execute(
                "SELECT entities_json, annoyance_score FROM classifications WHERE post_id = ?",
                (pid,),
            ).fetchone()
        self.assertIsNotNone(c)
        self.assertEqual(c["annoyance_score"], 85.0)
        entities = json.loads(c["entities_json"])
        self.assertEqual(entities[0]["name"], "United Airlines")

    def test_scrub_leaves_fresh_posts_alone(self):
        pid = "unit:retention:fresh"
        db.insert_post(
            id=pid, source="unit",
            content="Fresh post, still readable", posted_at="unused",
            author="bob",
        )
        _backdate(pid, 5)
        scrubbed = db.scrub_raw_content_older_than(days=30)
        self.assertEqual(scrubbed, 0)

        with db.cursor() as cur:
            row = cur.execute(
                "SELECT content, author, content_dropped_at FROM posts WHERE id = ?",
                (pid,),
            ).fetchone()
        self.assertEqual(row["content"], "Fresh post, still readable")
        self.assertEqual(row["author"], "bob")
        self.assertIsNone(row["content_dropped_at"])

    def test_scrub_is_idempotent(self):
        pid = "unit:retention:idem"
        db.insert_post(id=pid, source="unit",
                       content="Text", posted_at="unused", author="author")
        _backdate(pid, 40)
        self.assertEqual(db.scrub_raw_content_older_than(days=30), 1)
        # Second run should scrub nothing — content_dropped_at now non-null
        self.assertEqual(db.scrub_raw_content_older_than(days=30), 0)

    def test_join_still_works_after_scrub(self):
        """Aggregator / spike_detector JOIN posts ↔ classifications. After
        scrub, content is '' not NULL, so the JOIN and the entity-content
        LIKE filter still return the row. This simulates that query shape."""
        pid = "unit:retention:join"
        db.insert_post(id=pid, source="unit",
                       content="Apple ate my data", posted_at="unused")
        _backdate(pid, 45)
        db.insert_classification(
            post_id=pid, annoyance_score=80.0, sentiment="angry",
            primary_topic=None,
            entities=[{"name": "Apple", "type": "company", "salience": 0.9}],
            model="unit-test",
        )
        db.scrub_raw_content_older_than(days=30)

        with db.cursor() as cur:
            row = cur.execute(
                """SELECT c.post_id, c.entities_json, p.source, p.content
                   FROM classifications c JOIN posts p ON p.id = c.post_id
                   WHERE c.post_id = ?""",
                (pid,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["content"], "")  # scrubbed, but still joins
        entities = json.loads(row["entities_json"])
        self.assertEqual(entities[0]["name"], "Apple")


if __name__ == "__main__":
    unittest.main()
