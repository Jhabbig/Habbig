"""Standalone tests for the accuracy backtest.

Run with: python3 test_accuracy.py

Covers:
  - Brier score math (perfect, coinflip, maximally wrong)
  - Hit rate computation
  - Calibration-50 bucket logic
  - Year extraction from race_key
  - Filtering by race_type and min_year
  - The curated dataset seeds without errors and produces sensible stats
  - DB upsert idempotency
  - Single-source badge endpoint shape
"""
from __future__ import annotations

import sys
import sqlite3 as _sql


def passed(label: str) -> None:
    print(f"PASS {label}")


def fail(label: str, detail: str = "") -> None:
    print(f"FAIL {label}: {detail}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 1. Brier score math
# ---------------------------------------------------------------------------

from accuracy import _brier, _is_hit, _year_from_race_key, compute_source_stats, compute_summary

# Perfect prediction: assigned 1.0 to the winner
if abs(_brier(1.0) - 0.0) > 1e-9:
    fail("brier: perfect prediction = 0", str(_brier(1.0)))
passed("brier: perfect prediction = 0")

# Coinflip: assigned 0.5 — Brier should be 0.25
if abs(_brier(0.5) - 0.25) > 1e-9:
    fail("brier: 0.5 prediction = 0.25", str(_brier(0.5)))
passed("brier: 0.5 prediction = 0.25")

# Maximally wrong: assigned 0.0 to the winner
if abs(_brier(0.0) - 1.0) > 1e-9:
    fail("brier: 0 prediction = 1", str(_brier(0.0)))
passed("brier: 0.0 prediction = 1.0")

# Clamping: out-of-range inputs don't crash
if abs(_brier(-0.5) - 1.0) > 1e-9:
    fail("brier: negative input clamps to 0")
if abs(_brier(1.5) - 0.0) > 1e-9:
    fail("brier: >1 input clamps to 1")
passed("brier: clamps out-of-range inputs")

# Hit rate threshold
if not _is_hit(0.5):
    fail("is_hit: exactly 0.5 counts as a hit (tie-breaker)")
if _is_hit(0.49):
    fail("is_hit: 0.49 is NOT a hit")
if not _is_hit(0.51):
    fail("is_hit: 0.51 IS a hit")
passed("is_hit: threshold logic at 0.5")


# ---------------------------------------------------------------------------
# 2. Year extraction
# ---------------------------------------------------------------------------

if _year_from_race_key("senate_GA_2020") != 2020:
    fail("year_from_race_key: standard senate_GA_2020")
if _year_from_race_key("presidential_US_2024") != 2024:
    fail("year_from_race_key: presidential_US_2024")
if _year_from_race_key("senate_GA_special_2020") != 2020:
    fail("year_from_race_key: special variant")
if _year_from_race_key("senate_GA") != 0:
    fail("year_from_race_key: no year returns 0")
if _year_from_race_key("") != 0:
    fail("year_from_race_key: empty returns 0")
passed("year_from_race_key: extracts year, returns 0 when absent")


# ---------------------------------------------------------------------------
# 3. compute_source_stats end-to-end on a synthetic dataset
# ---------------------------------------------------------------------------

synthetic = [
    # source A: 3/3 hits, very confident — should be excellent
    {"race_key": "senate_X_2020", "source": "A", "closing_prob": 0.90, "race_type": "senate"},
    {"race_key": "senate_Y_2022", "source": "A", "closing_prob": 0.85, "race_type": "senate"},
    {"race_key": "senate_Z_2024", "source": "A", "closing_prob": 0.95, "race_type": "senate"},
    # source B: 1/3 hits, often wrong — should be poor
    {"race_key": "senate_X_2020", "source": "B", "closing_prob": 0.40, "race_type": "senate"},
    {"race_key": "senate_Y_2022", "source": "B", "closing_prob": 0.20, "race_type": "senate"},
    {"race_key": "senate_Z_2024", "source": "B", "closing_prob": 0.55, "race_type": "senate"},
]
stats = compute_source_stats(synthetic)

a = stats.get("A")
if a is None:
    fail("compute_source_stats: source A missing")
if a["n"] != 3:
    fail("source A: n=3", str(a))
if a["hit_rate"] != 1.0:
    fail("source A: 3/3 hits = 1.0", str(a))
# Brier for A: ((1-0.9)² + (1-0.85)² + (1-0.95)²) / 3 = (0.01 + 0.0225 + 0.0025) / 3 ≈ 0.0117
expected_brier_a = ((0.10)**2 + (0.15)**2 + (0.05)**2) / 3
if abs(a["brier"] - round(expected_brier_a, 4)) > 1e-3:
    fail("source A: Brier math", f"got {a['brier']}, expected ~{expected_brier_a}")
passed("compute_source_stats: source A (3/3 confident hits) → hit_rate=1.0, low Brier")

b = stats.get("B")
if b["hit_rate"] != round(1/3, 4):
    fail("source B: 1/3 hits", str(b))
# B has 2 toss-ups in [0.40, 0.60]: 0.40 (miss, < 0.5) and 0.55 (hit, >= 0.5).
# 1 hit / 2 toss-ups = 0.5 — exactly what a well-calibrated toss-up forecaster
# should produce.
if b["calibration_50"] != 0.5 or b["n_toss_ups"] != 2:
    fail("source B: toss-up calibration (2 toss-ups, 1 hit = 0.5)", str(b))
passed("compute_source_stats: source B (1/3 hits, 2 toss-ups) → calibration_50=0.5")


# ---------------------------------------------------------------------------
# 4. Filtering by race_type and min_year
# ---------------------------------------------------------------------------

mixed = [
    {"race_key": "senate_X_2020", "source": "A", "closing_prob": 0.90, "race_type": "senate"},
    {"race_key": "governor_Y_2022", "source": "A", "closing_prob": 0.10, "race_type": "governor"},
    {"race_key": "senate_Z_2024", "source": "A", "closing_prob": 0.85, "race_type": "senate"},
]
sen_only = compute_source_stats(mixed, race_type="senate")
if sen_only["A"]["n"] != 2:
    fail("filter: race_type=senate keeps 2 rows", str(sen_only))
if sen_only["A"]["hit_rate"] != 1.0:
    fail("filter: senate-only hit rate", str(sen_only))
passed("filter: race_type=senate restricts to senate rows")

since_2024 = compute_source_stats(mixed, min_year=2024)
if since_2024["A"]["n"] != 1:
    fail("filter: min_year=2024 keeps 1 row", str(since_2024))
passed("filter: min_year=2024 restricts to 2024+ rows")


# ---------------------------------------------------------------------------
# 5. Curated dataset seeds and produces sensible stats
# ---------------------------------------------------------------------------

from database import Database, DB_PATH
from accuracy import seed_from_curated_dataset

_db = Database()
_db.connect()
# Clear any prior seed state so we're testing the real seeding behaviour
with _sql.connect(DB_PATH) as _c:
    _c.execute("DELETE FROM midterm_historical_predictions WHERE race_key LIKE '%_20__'")
    _c.execute("DELETE FROM midterm_race_resolutions WHERE notes LIKE 'Seeded from accuracy_backfill.py'")
    _c.commit()

n_res, n_pred = seed_from_curated_dataset(_db)
if n_res < 30:
    fail("seed: at least 30 resolutions in curated dataset", f"got {n_res}")
if n_pred < 80:
    fail("seed: at least 80 predictions in curated dataset", f"got {n_pred}")
passed(f"seed: curated dataset loads ({n_res} resolutions, {n_pred} predictions)")

# Idempotency: a second seed call shouldn't double-write
n_res2, n_pred2 = seed_from_curated_dataset(_db)
if n_res2 != n_res or n_pred2 != n_pred:
    fail("seed: idempotent")
# Confirm the row count in the DB matches (didn't duplicate)
with _sql.connect(DB_PATH) as _c:
    actual_preds = _c.execute("SELECT COUNT(*) FROM midterm_historical_predictions").fetchone()[0]
if actual_preds != n_pred:
    fail("seed: idempotent at DB level", f"db has {actual_preds}, seeded {n_pred}")
passed("seed: idempotent (upsert, not insert)")

# Stats over the real dataset should look reasonable
predictions = _db.get_historical_predictions()
summary = compute_summary(predictions)

# Every source in the curated dataset should appear
expected_sources = {"polymarket", "predictit", "polling"}  # kalshi only 2024
for src in expected_sources:
    if src not in summary["overall"]:
        fail(f"summary.overall: source '{src}' present", str(summary["overall"]))
passed("summary: all major sources present in overall stats")

# Hit rates should be plausible (between 0.5 and 1.0 for prediction markets on
# historical races — they were generally right). Don't be too strict on numbers
# since the dataset can be refined, but catch the case where everything is at 0.
poly = summary["overall"].get("polymarket", {})
if not poly or poly.get("hit_rate", 0) < 0.5:
    fail("summary: polymarket hit rate >= 0.5 on resolved historical races", str(poly))
if poly.get("brier") is None or poly["brier"] > 0.30:
    fail("summary: polymarket Brier < 0.30 (better than coinflip)", str(poly))
passed("summary: polymarket has hit_rate >= 0.5 and Brier < 0.30 on historicals")

# By-race-type breakdown should include senate, governor, presidential
by_rt = summary["by_race_type"]
for rt in ("senate", "governor", "presidential"):
    if rt not in by_rt:
        fail(f"summary.by_race_type: '{rt}' missing", str(list(by_rt.keys())))
passed("summary.by_race_type: senate, governor, presidential all present")


# ---------------------------------------------------------------------------
# 6. /data/accuracy/badge/{source} endpoint shape
# ---------------------------------------------------------------------------

import main as main_mod
import asyncio

main_mod.state.db = _db


async def _badge_test():
    result = await main_mod.data_accuracy_badge("polymarket", race_type="senate")
    if not result.get("available"):
        fail("badge: polymarket+senate available", str(result))
    for key in ("source", "n", "hit_rate", "brier"):
        if key not in result:
            fail(f"badge: response missing '{key}'", str(result))
    if result["source"] != "polymarket" or result["race_type"] != "senate":
        fail("badge: echoes back source + race_type", str(result))
    passed("badge: returns full shape for known (source, race_type)")

    # Unknown source returns available=False, not a 500
    result2 = await main_mod.data_accuracy_badge("nonexistent_source")
    if result2.get("available") is not False:
        fail("badge: unknown source returns available=False", str(result2))
    passed("badge: unknown source returns available=False cleanly")


asyncio.run(_badge_test())


# ---------------------------------------------------------------------------
# 7. Routes registered
# ---------------------------------------------------------------------------

paths = {r.path for r in main_mod.app.routes if hasattr(r, "path")}
required = {"/data/accuracy", "/data/accuracy/badge/{source}"}
missing = required - paths
if missing:
    fail("routes: accuracy endpoints registered", f"missing {missing}")
passed("routes: /data/accuracy + /data/accuracy/badge/{source} registered")


print("\nAll accuracy tests passed.")
