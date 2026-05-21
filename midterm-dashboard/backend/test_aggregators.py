"""Standalone tests for the new aggregators (Manifold + Metaculus).

Run with: python3 test_aggregators.py

Covers:
  - Manifold: normalizes BINARY and MULTIPLE_CHOICE markets
  - Manifold: filters out resolved + non-2026 + non-political markets
  - Manifold: extracts state from question, classifies race_type
  - Metaculus: normalizes binary politics questions
  - Metaculus: pulls community median from new + legacy schema shapes
  - Metaculus: filters out non-binary + non-2026 questions
  - Both: skip markets missing a probability cleanly
  - Divergence schema migration: manifold/metaculus columns exist
  - Source-column map drives all per-source iteration in main.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone, timedelta


def passed(label: str) -> None:
    print(f"PASS {label}")


def fail(label: str, detail: str = "") -> None:
    print(f"FAIL {label}: {detail}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 1. Manifold normalization
# ---------------------------------------------------------------------------

from aggregators.manifold import ManifoldAggregator, _extract_state, _classify_race_type, _is_open

# Race-type classifier
for q, expected in [
    ("2026 Georgia Senate election", "senate"),
    ("2026 Pennsylvania House race CD-1", "house"),
    ("2026 Texas gubernatorial race", "governor"),
    # The "control" branch comes first so chamber-control markets aren't
    # misclassified as per-state senate/house races. Both of these mention
    # a chamber but are fundamentally about who CONTROLS it.
    ("Will Republicans control the House after 2026?", "control"),
    ("Who controls the Senate in 2026?", "control"),
    ("Will Democrats flip the Senate in 2026?", "control"),
    ("Will the dog catch the cat?", "other"),
]:
    if _classify_race_type(q) != expected:
        fail(f"_classify_race_type({q!r})", f"expected {expected}, got {_classify_race_type(q)}")
passed("manifold._classify_race_type: senate/house/governor/control/other")

# State extraction
if _extract_state("2026 Pennsylvania Senate race") != "PA":
    fail("_extract_state: Pennsylvania")
if _extract_state("Washington D.C. statehood by 2026?") is not None:
    fail("_extract_state: D.C. should NOT match Washington state")
if _extract_state("North Carolina governor 2026") != "NC":
    fail("_extract_state: North Carolina")
passed("manifold._extract_state: full names + D.C. exception")

# _is_open
future = int((datetime.now(timezone.utc) + timedelta(days=180)).timestamp() * 1000)
past = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
if not _is_open({"closeTime": future, "isResolved": False}):
    fail("_is_open: future close + unresolved → True")
if _is_open({"closeTime": past, "isResolved": False}):
    fail("_is_open: past close → False")
if _is_open({"closeTime": future, "isResolved": True}):
    fail("_is_open: resolved → False")
passed("manifold._is_open: respects isResolved + closeTime")

# Normalize binary
agg = ManifoldAggregator()
binary_raw = [{
    "id": "abc123",
    "question": "Will Democrats win the 2026 Pennsylvania Senate race?",
    "outcomeType": "BINARY",
    "probability": 0.62,
    "volume": 12500,
    "totalLiquidity": 800,
    "isResolved": False,
    "closeTime": future,
    "url": "https://manifold.markets/u/x/will-dems-win-pa-senate-2026",
}]
norm = agg._normalize(binary_raw)
if len(norm) != 1:
    fail("manifold normalize: binary market produces 1 normalized row", str(norm))
n = norm[0]
if n["source"] != "manifold" or n["race_type"] != "senate" or n["state"] != "PA":
    fail("manifold normalize: source/race_type/state on PA senate", str(n))
if len(n["outcomes"]) != 2 or n["outcomes"][0]["name"] != "Yes":
    fail("manifold normalize: binary produces Yes+No outcomes", str(n["outcomes"]))
if abs(n["outcomes"][0]["probability"] - 0.62) > 1e-9 or abs(n["outcomes"][1]["probability"] - 0.38) > 1e-9:
    fail("manifold normalize: Yes prob + No prob complementary", str(n["outcomes"]))
passed("manifold normalize: BINARY → Yes/No outcomes with complementary probs")

# Multiple-choice market with explicit answers
mc_raw = [{
    "id": "def456",
    "question": "Who wins the 2026 Ohio Senate race?",
    "outcomeType": "MULTIPLE_CHOICE",
    "answers": [
        {"id": "a1", "text": "Republican", "probability": 0.55},
        {"id": "a2", "text": "Democrat", "probability": 0.40},
        {"id": "a3", "text": "Independent", "probability": 0.05},
    ],
    "volume": 5000,
    "isResolved": False,
    "closeTime": future,
}]
norm = agg._normalize(mc_raw)
if len(norm) != 1 or len(norm[0]["outcomes"]) != 3:
    fail("manifold normalize: multiple choice keeps all 3 outcomes", str(norm))
if norm[0]["state"] != "OH":
    fail("manifold normalize: OH state extracted", str(norm))
passed("manifold normalize: MULTIPLE_CHOICE → N outcomes with per-answer probs")

# Filter cases
filter_cases = [
    {"id": "r1", "question": "2026 Senate Georgia", "isResolved": True, "outcomeType": "BINARY",
     "probability": 0.5, "closeTime": future},                     # resolved
    {"id": "r2", "question": "2024 Senate Georgia", "outcomeType": "BINARY",
     "probability": 0.5, "isResolved": False, "closeTime": future},  # not 2026
    {"id": "r3", "question": "Will it rain in 2026?", "outcomeType": "BINARY",
     "probability": 0.5, "isResolved": False, "closeTime": future},  # not political
    {"id": "r4", "question": "2026 Senate Georgia", "outcomeType": "PSEUDO_NUMERIC",
     "probability": 0.5, "isResolved": False, "closeTime": future},  # unsupported type
    {"id": "r5", "question": "2026 Senate Georgia", "outcomeType": "BINARY",
     "probability": None, "isResolved": False, "closeTime": future},  # missing prob
]
norm = agg._normalize(filter_cases)
if norm:
    fail("manifold normalize: all filter cases excluded", str(norm))
passed("manifold normalize: drops resolved/non-2026/non-political/unsupported/missing-prob")


# ---------------------------------------------------------------------------
# 2. Metaculus normalization
# ---------------------------------------------------------------------------

from aggregators.metaculus import MetaculusAggregator, _community_yes_prob

# Legacy schema: community_prediction.full.q2
q_legacy = {"community_prediction": {"full": {"q2": 0.72}}}
if _community_yes_prob(q_legacy) != 0.72:
    fail("metaculus._community_yes_prob: legacy q2 path")
# New aggregations schema
q_new = {"aggregations": {"recency_weighted": {"latest": {"centers": [0.41]}}}}
if _community_yes_prob(q_new) != 0.41:
    fail("metaculus._community_yes_prob: new aggregations path")
# Out-of-range protection
q_bad = {"community_prediction": {"full": {"q2": 1.5}}}
if _community_yes_prob(q_bad) is not None:
    fail("metaculus._community_yes_prob: rejects out-of-range probability")
# Missing entirely
if _community_yes_prob({}) is not None:
    fail("metaculus._community_yes_prob: empty question returns None")
passed("metaculus._community_yes_prob: handles both schema versions + bounds + missing")

agg_m = MetaculusAggregator()
metaculus_raw = [
    {
        "id": 1234,
        "title": "Will Democrats win the 2026 Wisconsin Senate race?",
        "possibilities": {"type": "binary"},
        "community_prediction": {"full": {"q2": 0.55}},
        "page_url": "/questions/1234/",
        "close_time": "2026-11-04T00:00:00Z",
    },
    # Non-political question with 2026 → should NOT be filtered just because it's non-political?
    # Actually our classifier returns "other" so it should be filtered.
    {
        "id": 5,
        "title": "Will GDP hit X by end of 2026?",
        "possibilities": {"type": "binary"},
        "community_prediction": {"full": {"q2": 0.30}},
    },
    # 2028 question
    {
        "id": 6,
        "title": "Will Democrats win the 2028 senate?",
        "possibilities": {"type": "binary"},
        "community_prediction": {"full": {"q2": 0.50}},
    },
    # Numeric question (not binary)
    {
        "id": 7,
        "title": "2026 Texas Senate vote share for Democrat",
        "possibilities": {"type": "numeric"},
        "community_prediction": {"full": {"q2": 0.45}},
    },
]
norm = agg_m._normalize(metaculus_raw)
if len(norm) != 1:
    fail("metaculus normalize: only the binary 2026-political question kept", str([n["title"] for n in norm]))
n = norm[0]
if n["source"] != "metaculus" or n["race_type"] != "senate" or n["state"] != "WI":
    fail("metaculus normalize: source/race_type/state on WI senate", str(n))
if abs(n["outcomes"][0]["probability"] - 0.55) > 1e-9:
    fail("metaculus normalize: Yes prob from community median", str(n["outcomes"]))
if n["volume"] != 0.0 or n["liquidity"] != 0.0:
    fail("metaculus normalize: no volume/liquidity (forecasting platform)", str(n))
passed("metaculus normalize: keeps binary 2026-political, drops everything else")


# ---------------------------------------------------------------------------
# 3. Aggregator init + close idempotency
# ---------------------------------------------------------------------------

async def _close_test():
    a = ManifoldAggregator()
    await a.close()  # never opened — should be no-op
    b = MetaculusAggregator()
    await b.close()
    passed("aggregators: close() is safe when session was never opened")


asyncio.run(_close_test())


# ---------------------------------------------------------------------------
# 4. Schema migration: manifold_prob + metaculus_prob columns exist
# ---------------------------------------------------------------------------

from database import Database, DB_PATH
import sqlite3 as _sql

db = Database()
db.connect()

with _sql.connect(DB_PATH) as c:
    cols = {row[1] for row in c.execute("PRAGMA table_info(midterm_divergence_snapshots)").fetchall()}
if "manifold_prob" not in cols or "metaculus_prob" not in cols:
    fail("schema: divergence snapshots gained manifold + metaculus columns", str(sorted(cols)))
passed("schema: midterm_divergence_snapshots has manifold_prob + metaculus_prob")

# Round-trip a divergence write with the new sources
db.record_divergence(
    race_key="senate_TEST", state="TS", race_type="senate",
    data={
        "polymarket": 0.55, "kalshi": 0.56, "predictit": 0.54,
        "polling": 0.50, "manifold": 0.58, "metaculus": 0.52,
        "max_divergence": 0.08, "details": {},
    },
)
with _sql.connect(DB_PATH) as c:
    row = c.execute(
        "SELECT manifold_prob, metaculus_prob FROM midterm_divergence_snapshots WHERE race_key='senate_TEST'"
    ).fetchone()
if not row or abs(row[0] - 0.58) > 1e-9 or abs(row[1] - 0.52) > 1e-9:
    fail("schema: manifold/metaculus values persist through write", str(row))
passed("schema: record_divergence writes + reads manifold_prob + metaculus_prob")

# Cleanup
with _sql.connect(DB_PATH) as c:
    c.execute("DELETE FROM midterm_divergence_snapshots WHERE race_key='senate_TEST'")
    c.commit()


# ---------------------------------------------------------------------------
# 5. main.py uses ALL_SOURCES + DIVERGENCE_COL
# ---------------------------------------------------------------------------

import main as main_mod
if main_mod.ALL_SOURCES != ("polymarket", "kalshi", "predictit", "polling", "manifold", "metaculus"):
    fail("main.ALL_SOURCES: includes all 6 in canonical order", str(main_mod.ALL_SOURCES))
if main_mod.DIVERGENCE_COL["polling"] != "polling_avg":
    fail("main.DIVERGENCE_COL: polling maps to polling_avg (not polling_prob)")
if main_mod.DIVERGENCE_COL["manifold"] != "manifold_prob":
    fail("main.DIVERGENCE_COL: manifold maps to manifold_prob")
passed("main.ALL_SOURCES + DIVERGENCE_COL: canonical 6-source ordering with polling_avg fix")


print("\nAll aggregator tests passed.")
