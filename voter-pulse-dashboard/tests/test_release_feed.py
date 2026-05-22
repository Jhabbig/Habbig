"""Tests for the 'what changed' release feed compose."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import release_feed  # noqa: E402


def _series(sid, label, units, hib, points, yoy=None):
    return {"series_id": sid, "label": label, "units": units, "higher_is_better": hib,
            "group": "g", "points": points,
            "latest": points[-1] if points else None, "yoy_pct": yoy}


class ReleaseFeedTests(unittest.TestCase):
    def setUp(self):
        # Pin "now" so the days_ago math is deterministic.
        self.fixed_now = datetime(2026, 5, 22, tzinfo=timezone.utc)

    def _compose(self, life):
        with patch.object(release_feed, "datetime") as dt_mock:
            dt_mock.now.return_value = self.fixed_now
            # Datetime methods we use need pass-through
            dt_mock.strptime = datetime.strptime
            return release_feed.compose(life)

    def test_recent_observation_appears_in_feed(self):
        recent = self.fixed_now - timedelta(days=5)
        life = {"series": [_series("X", "X", "%", False,
                                    [{"date": "2026-04-01", "value": 100},
                                     {"date": recent.strftime("%Y-%m-%d"), "value": 102}])]}
        out = self._compose(life)
        self.assertEqual(len(out["releases"]), 1)
        r = out["releases"][0]
        self.assertEqual(r["series_id"], "X")
        self.assertAlmostEqual(r["pop_change"], 2.0, places=2)
        self.assertEqual(r["days_ago"], 5)

    def test_stale_observation_moves_to_stale_list(self):
        old_date = (self.fixed_now - timedelta(days=400)).strftime("%Y-%m-%d")
        life = {"series": [_series("OLD", "old", "%", False,
                                    [{"date": old_date, "value": 5.0}])]}
        out = self._compose(life)
        self.assertEqual(len(out["releases"]), 0)
        self.assertEqual(out["stale_count"], 1)
        self.assertEqual(out["stale"][0]["series_id"], "OLD")

    def test_sort_newest_first(self):
        d1 = (self.fixed_now - timedelta(days=2)).strftime("%Y-%m-%d")
        d2 = (self.fixed_now - timedelta(days=20)).strftime("%Y-%m-%d")
        life = {"series": [
            _series("OLDER",  "older",  "%", False, [{"date": d2, "value": 1.0}]),
            _series("NEWER",  "newer",  "%", False, [{"date": d1, "value": 1.0}]),
        ]}
        out = self._compose(life)
        self.assertEqual(out["releases"][0]["series_id"], "NEWER")
        self.assertEqual(out["releases"][1]["series_id"], "OLDER")

    def test_human_ago_buckets(self):
        now = datetime(2026, 5, 22, tzinfo=timezone.utc)
        cases = [
            (now,                              "today"),
            (now - timedelta(days=1),          "yesterday"),
            (now - timedelta(days=4),          "4 days ago"),
            (now - timedelta(days=10),         "last week"),
            (now - timedelta(days=21),         "3 weeks ago"),
            (now - timedelta(days=60),         "about 2 months ago"),
        ]
        for then, expected in cases:
            self.assertEqual(release_feed._human_ago(then, now), expected)


if __name__ == "__main__":
    unittest.main()
