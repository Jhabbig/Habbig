"""
Regression harness for the two-pass classifier.

Requires ANTHROPIC_API_KEY. Skipped otherwise so the rest of the unit
suite stays green locally.

Run manually after every prompt change:

    ANTHROPIC_API_KEY=... python -m pytest tests/test_classifier_regression.py -v
    # verbose per-post diff:
    ANTHROPIC_API_KEY=... python -m pytest tests/test_classifier_regression.py -v -s

Hard thresholds (from the plan):
  - MAE on annoyance_score ≤ 15
  - F1 on entity name extraction ≥ 0.70
  - Type accuracy (entity.type) ≥ 0.75
  - is_sensitive F1 ≥ 0.60

Fails if any threshold is violated so CI can gate prompt changes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Iterable

THIS_DIR = Path(__file__).resolve().parent
ROOT = THIS_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE_PATH = THIS_DIR / "fixtures" / "labeled_posts.jsonl"

# Thresholds
MAE_MAX = 15.0
ENTITY_F1_MIN = 0.70
TYPE_ACC_MIN = 0.75
SENSITIVE_F1_MIN = 0.60


def _load_fixtures(path: Path = FIXTURE_PATH) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _mae(actual: Iterable[float], expected: Iterable[float]) -> float:
    actual = list(actual)
    expected = list(expected)
    if not actual:
        return 0.0
    return sum(abs(a - e) for a, e in zip(actual, expected)) / len(actual)


def _entity_name_f1(actual_entities: list[list[dict]], expected_entities: list[list[dict]]) -> float:
    """Case-insensitive exact-match F1 on entity names, micro-averaged over posts."""
    tp = fp = fn = 0
    for act, exp in zip(actual_entities, expected_entities):
        act_names = {(e.get("name") or "").strip().lower() for e in act}
        exp_names = {(e.get("name") or "").strip().lower() for e in exp}
        tp += len(act_names & exp_names)
        fp += len(act_names - exp_names)
        fn += len(exp_names - act_names)
    if tp == 0 and (fp > 0 or fn > 0):
        return 0.0
    if tp == 0:
        return 1.0 if fp == 0 and fn == 0 else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _type_accuracy(actual_entities: list[list[dict]], expected_entities: list[list[dict]]) -> float:
    """For every expected entity that the classifier also extracted, count
    whether entity.type matches. Ignores FPs so it isn't double-counted
    against the F1 metric."""
    matches = total = 0
    for act, exp in zip(actual_entities, expected_entities):
        act_by_name = {(e.get("name") or "").strip().lower(): e.get("type") for e in act}
        for e in exp:
            name = (e.get("name") or "").strip().lower()
            exp_type = e.get("type")
            if name in act_by_name:
                total += 1
                if act_by_name[name] == exp_type:
                    matches += 1
    if total == 0:
        return 1.0
    return matches / total


def _binary_f1(actual: list[bool], expected: list[bool]) -> float:
    tp = sum(1 for a, e in zip(actual, expected) if a and e)
    fp = sum(1 for a, e in zip(actual, expected) if a and not e)
    fn = sum(1 for a, e in zip(actual, expected) if not a and e)
    if tp == 0 and fp == 0 and fn == 0:
        return 1.0  # nothing to predict either way
    if tp == 0:
        return 0.0
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


@unittest.skipUnless(os.environ.get("ANTHROPIC_API_KEY"), "live API required")
class TestClassifierRegression(unittest.TestCase):
    """Live Claude regression harness. Only runs with a real API key."""

    @classmethod
    def setUpClass(cls):
        # Point at a throwaway DB so we don't pollute annoyance.db with fixtures.
        import tempfile
        cls._tmp_dir = tempfile.mkdtemp(prefix="annoyance-regression-")
        cls._tmp_db = Path(cls._tmp_dir) / "regression.db"

        import config
        config.DB_PATH = cls._tmp_db  # redirect before first connection

        import db as _db
        _db._local.__dict__.clear()  # force new connection pointed at tmp db
        _db.init_db()
        cls.db = _db

        cls.fixtures = _load_fixtures()
        # Seed posts so classify_pending_posts has something to pull.
        for f in cls.fixtures:
            cls.db.insert_post(
                id=f["id"], source="fixture", content=f["content"],
                posted_at="2026-04-20T00:00:00+00:00",
            )

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls._tmp_dir, ignore_errors=True)

    def test_regression(self):
        """Runs the classifier on all fixtures and asserts threshold metrics."""
        from classifier import classify_pending_posts

        # Drain all fixtures through the two-pass pipeline. The batches are
        # CLASSIFIER_BATCH_SIZE=50 so 100 fixtures → 2 ticks.
        async def run():
            total = {"triaged": 0, "classified": 0, "skipped": 0}
            for _ in range(5):  # safety: up to 5 batches
                summary = await classify_pending_posts(limit=50)
                total["triaged"] += summary.get("triaged", 0)
                total["classified"] += summary.get("classified", 0)
                total["skipped"] += summary.get("skipped", 0)
                if summary.get("triaged", 0) == 0:
                    break
            return total

        total = asyncio.run(run())
        print(f"\nPipeline totals: {total}")

        # Pull actual classifications back out of the DB.
        with self.db.cursor() as cur:
            rows = cur.execute(
                "SELECT post_id, annoyance_score, sentiment, entities_json, is_sensitive "
                "FROM classifications"
            ).fetchall()
        by_id = {r["post_id"]: dict(r) for r in rows}

        actual_annoyance = []
        expected_annoyance = []
        actual_entities = []
        expected_entities = []
        actual_sensitive = []
        expected_sensitive = []
        missing = []
        verbose = any(arg in sys.argv for arg in ("-s", "--capture=no"))

        for f in self.fixtures:
            act = by_id.get(f["id"])
            if not act:
                missing.append(f["id"])
                continue
            try:
                ent = json.loads(act["entities_json"] or "[]")
            except Exception:
                ent = []
            actual_annoyance.append(float(act["annoyance_score"]))
            expected_annoyance.append(float(f["expected_annoyance"]))
            actual_entities.append(ent)
            expected_entities.append(f["expected_entities"])
            actual_sensitive.append(bool(act["is_sensitive"]))
            expected_sensitive.append(bool(f["expected_is_sensitive"]))

            if verbose:
                diff = abs(float(act["annoyance_score"]) - float(f["expected_annoyance"]))
                act_names = sorted({(e.get("name") or "") for e in ent})
                exp_names = sorted({(e.get("name") or "") for e in f["expected_entities"]})
                print(
                    f"[{f['id']}] anno {float(act['annoyance_score']):5.1f} vs {float(f['expected_annoyance']):5.1f} "
                    f"(Δ{diff:4.1f}) entities actual={act_names} expected={exp_names}"
                )

        if missing:
            print(f"WARNING: {len(missing)} fixtures missing from classifier output: {missing[:5]}...")

        mae = _mae(actual_annoyance, expected_annoyance)
        entity_f1 = _entity_name_f1(actual_entities, expected_entities)
        type_acc = _type_accuracy(actual_entities, expected_entities)
        sensitive_f1 = _binary_f1(actual_sensitive, expected_sensitive)

        print(f"\n=== Regression Metrics ===")
        print(f"  MAE (annoyance_score): {mae:.2f}  (max {MAE_MAX})")
        print(f"  Entity name F1:        {entity_f1:.3f}  (min {ENTITY_F1_MIN})")
        print(f"  Type accuracy:         {type_acc:.3f}  (min {TYPE_ACC_MIN})")
        print(f"  is_sensitive F1:       {sensitive_f1:.3f}  (min {SENSITIVE_F1_MIN})")
        print(f"  Fixtures:              {len(actual_annoyance)}/{len(self.fixtures)} classified")

        self.assertLessEqual(mae, MAE_MAX,
                             f"Annoyance MAE {mae:.2f} > {MAE_MAX}")
        self.assertGreaterEqual(entity_f1, ENTITY_F1_MIN,
                                f"Entity F1 {entity_f1:.3f} < {ENTITY_F1_MIN}")
        self.assertGreaterEqual(type_acc, TYPE_ACC_MIN,
                                f"Type accuracy {type_acc:.3f} < {TYPE_ACC_MIN}")
        self.assertGreaterEqual(sensitive_f1, SENSITIVE_F1_MIN,
                                f"Sensitive F1 {sensitive_f1:.3f} < {SENSITIVE_F1_MIN}")


if __name__ == "__main__":
    unittest.main()
