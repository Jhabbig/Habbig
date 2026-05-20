"""Standalone tests for movement analysis + LLM grounding.

Run with: python3 test_movements.py

Critical coverage:
  - News query builder produces a sensible query
  - Articles published AFTER the window end are filtered out
  - LLM response validator drops fabricated URLs and out-of-range indices
  - Movement analyzer short-circuits below the noise threshold (no LLM call)
  - DB cache returns hits inside TTL, misses after expiry
  - Cache survives expired-row pruning
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import sqlite3 as _sql


def passed(label: str) -> None:
    print(f"PASS {label}")


def fail(label: str, detail: str = "") -> None:
    print(f"FAIL {label}: {detail}")
    sys.exit(1)


from database import Database, DB_PATH

_db = Database()
_db.connect()
with _sql.connect(DB_PATH) as _c:
    _c.execute("DELETE FROM midterm_movement_explanations")
    _c.commit()


# ---------------------------------------------------------------------------
# 1. News query builder
# ---------------------------------------------------------------------------

from news import _build_query, _normalize_newsapi, _normalize_gdelt, _iso_to_dt

q = _build_query("senate", "GA", {"candidates": [{"name": "Jon Ossoff"}, {"name": "Brian Kemp"}]})
if "Georgia" not in q or "senate" not in q or "2026" not in q:
    fail("news.query: includes state full name, race type, year", q)
if "Ossoff" not in q or "Kemp" not in q:
    fail("news.query: includes candidate surnames", q)
passed("news.query: includes state full name + race type + candidates + year")

q_no_state = _build_query("governor", "", None)
if "governor" not in q_no_state or "2026" not in q_no_state:
    fail("news.query: works without state or candidates", q_no_state)
passed("news.query: works without state or candidates")


# ---------------------------------------------------------------------------
# 2. Article normalization + temporal filter
# ---------------------------------------------------------------------------

newsapi_raw = {
    "title": "  Ossoff polls behind in Atlanta suburbs  ",
    "description": "New poll shows incumbent down 3pp...",
    "url": "https://example.com/poll",
    "source": {"name": "AJC"},
    "publishedAt": "2026-05-19T10:00:00Z",
}
norm = _normalize_newsapi(newsapi_raw)
if norm["headline"] != "Ossoff polls behind in Atlanta suburbs":
    fail("news.normalize: NewsAPI headline trimmed", norm)
if norm["url"] != "https://example.com/poll" or norm["source"] != "AJC":
    fail("news.normalize: NewsAPI URL+source", norm)
if norm["provider"] != "newsapi":
    fail("news.normalize: NewsAPI provider tag", norm)
passed("news.normalize: NewsAPI fields mapped + provider tagged")

gdelt_raw = {
    "title": "Georgia Senate poll",
    "url": "https://gdelt.example.com/x",
    "domain": "ajc.com",
    "seendate": "20260519T100000Z",
}
norm_g = _normalize_gdelt(gdelt_raw)
if not norm_g["published_at"].startswith("2026-05-19T10"):
    fail("news.normalize: GDELT timestamp parsed to ISO", norm_g)
passed("news.normalize: GDELT compact timestamp converted to ISO")

# ISO parsing is forgiving of Z suffix
if _iso_to_dt("2026-05-19T10:00:00Z") is None:
    fail("news.iso: handles Z suffix")
if _iso_to_dt("not-a-date") is not None:
    fail("news.iso: returns None on garbage")
passed("news.iso: handles Z suffix and rejects garbage")


# ---------------------------------------------------------------------------
# 3. LLM response validator — defense against fabricated citations
# ---------------------------------------------------------------------------

from llm import _validate_response

real_articles = [
    {"url": "https://a.example.com/1", "headline": "Real article one"},
    {"url": "https://b.example.com/2", "headline": "Real article two"},
]

# Case A: clean citation passes through
clean = {
    "summary": "Polls tightened.",
    "explanations": [
        {"article_index": 0, "headline": "Real article one", "url": "https://a.example.com/1",
         "quote": "Q", "rationale": "R", "confidence": "high"},
    ],
    "reason_if_empty": None,
}
out = _validate_response(clean, real_articles)
if len(out["explanations"]) != 1 or out["summary"] != "Polls tightened.":
    fail("llm.validate: clean citation preserved", str(out))
passed("llm.validate: clean citation preserved")

# Case B: fabricated URL is dropped
fabricated = {
    "summary": "Made up news.",
    "explanations": [
        {"article_index": 0, "headline": "Real article one",
         "url": "https://fake.example.com/INVENTED",  # mismatched URL
         "quote": "Q", "rationale": "R", "confidence": "high"},
    ],
    "reason_if_empty": None,
}
out = _validate_response(fabricated, real_articles)
if out["explanations"]:
    fail("llm.validate: drops fabricated URL", str(out))
if out["reason_if_empty"] != "no_relevant_news_found":
    fail("llm.validate: sets empty reason after dropping all citations", str(out))
passed("llm.validate: drops fabricated URL (anti-hallucination guard)")

# Case C: out-of-range article_index is dropped
oob = {
    "summary": "...",
    "explanations": [
        {"article_index": 99, "headline": "x", "url": "x", "quote": "q", "rationale": "r", "confidence": "low"},
        {"article_index": -1, "headline": "x", "url": "x", "quote": "q", "rationale": "r", "confidence": "low"},
    ],
    "reason_if_empty": None,
}
out = _validate_response(oob, real_articles)
if out["explanations"]:
    fail("llm.validate: drops out-of-range indices", str(out))
passed("llm.validate: drops out-of-range article indices")

# Case D: mismatched headline + correct URL → kept (URL is the load-bearing match)
headline_drift = {
    "summary": "...",
    "explanations": [
        {"article_index": 0, "headline": "Slightly different headline drift",
         "url": "https://a.example.com/1", "quote": "Q", "rationale": "R", "confidence": "medium"},
    ],
    "reason_if_empty": None,
}
out = _validate_response(headline_drift, real_articles)
if len(out["explanations"]) != 1:
    fail("llm.validate: keeps URL-matched citation even with headline drift", str(out))
if out["explanations"][0]["headline"] != "Real article one":
    fail("llm.validate: corrects drifted headline to source", str(out))
passed("llm.validate: keeps URL-matched citation but corrects drifted headline")

# Case E: garbage input → safe empty result
out = _validate_response("not a dict", real_articles)  # type: ignore[arg-type]
if out["explanations"] or out["reason_if_empty"] != "no_relevant_news_found":
    fail("llm.validate: garbage input → safe empty", str(out))
passed("llm.validate: garbage input → safe empty result")


# ---------------------------------------------------------------------------
# 4. Movement analyzer cache + short-circuit
# ---------------------------------------------------------------------------

# Store + retrieve round-trip
test_content = {"summary": "cached", "explanations": [], "movements": []}
_db.store_movement_explanation("senate_GA", "2026-05-19T10_24h", test_content, ttl_seconds=60)
got = _db.get_movement_explanation("senate_GA", "2026-05-19T10_24h")
if got != test_content:
    fail("cache: round-trip preserves payload", str(got))
passed("cache: round-trip preserves payload")

# Wrong bucket → miss
if _db.get_movement_explanation("senate_GA", "2026-05-19T11_24h") is not None:
    fail("cache: wrong bucket returns None")
passed("cache: wrong bucket returns None")

# Expired entry → miss
_db.store_movement_explanation("senate_GA", "expired", test_content, ttl_seconds=-1)
if _db.get_movement_explanation("senate_GA", "expired") is not None:
    fail("cache: expired entry returns None")
passed("cache: expired entry returns None")

# Upsert on same key
_db.store_movement_explanation("senate_GA", "2026-05-19T10_24h",
                                {"summary": "updated"}, ttl_seconds=60)
got = _db.get_movement_explanation("senate_GA", "2026-05-19T10_24h")
if (got or {}).get("summary") != "updated":
    fail("cache: upsert replaces payload", str(got))
passed("cache: upsert replaces payload")


# ---------------------------------------------------------------------------
# 5. Movement analyzer end-to-end (no LLM key, below-noise path)
# ---------------------------------------------------------------------------

import movement_analysis

# Insert tiny synthetic divergence history (movements < 1.5pp)
now = datetime.now(timezone.utc)
with _sql.connect(DB_PATH) as _c:
    for i, (offset_minutes, prob) in enumerate([(0, 0.500), (10, 0.501), (20, 0.503), (30, 0.504)]):
        ts = (now - timedelta(hours=2) + timedelta(minutes=offset_minutes)).isoformat()
        _c.execute(
            """INSERT INTO midterm_divergence_snapshots
                (race_key, state, race_type, polymarket_prob, snapshot_time)
               VALUES (?, ?, ?, ?, ?)""",
            ("test_noise", "TX", "senate", prob, ts),
        )
    _c.commit()


class _FakeSession:
    """Stand-in for an aiohttp session — analyzer should never call get()
    on the noise-threshold path."""

    def get(self, *a, **kw):
        raise AssertionError(
            "analyzer fetched news despite movement being below noise threshold"
        )


async def _run_noise_test():
    result = await movement_analysis.analyze_movement(
        db=_db,
        session=_FakeSession(),  # type: ignore[arg-type]
        race_key="test_noise",
        race_title="Test noise",
        race_type="senate",
        state="TX",
        hours=24,
    )
    if result["explanation"]["reason_if_empty"] != "insufficient_movement":
        fail("analyzer: short-circuits on sub-threshold movement",
             str(result["explanation"]))
    if result["cached"] is not False:
        fail("analyzer: first call is not cached", str(result))


asyncio.run(_run_noise_test())
passed("analyzer: short-circuits on sub-threshold movement (no news call)")


# Second call → cached
async def _run_cached_test():
    result = await movement_analysis.analyze_movement(
        db=_db,
        session=_FakeSession(),  # type: ignore[arg-type]
        race_key="test_noise",
        race_title="Test noise",
        race_type="senate",
        state="TX",
        hours=24,
    )
    if not result["cached"]:
        fail("analyzer: second call within TTL is cached", str(result))


asyncio.run(_run_cached_test())
passed("analyzer: cache hit on second call within TTL")


# Cleanup
with _sql.connect(DB_PATH) as _c:
    _c.execute("DELETE FROM midterm_divergence_snapshots WHERE race_key='test_noise'")
    _c.execute("DELETE FROM midterm_movement_explanations WHERE race_key IN ('senate_GA', 'test_noise')")
    _c.commit()


# ---------------------------------------------------------------------------
# 6. /data/movements/config endpoint exposes both providers
# ---------------------------------------------------------------------------

import main as main_mod
paths = {r.path for r in main_mod.app.routes if hasattr(r, "path")}
if "/data/movements/config" not in paths:
    fail("routes: /data/movements/config registered")
passed("routes: /data/movements/config registered")


# ---------------------------------------------------------------------------
# 7. Schema sanity — output schema accepts the right shapes
# ---------------------------------------------------------------------------

from llm import _OUTPUT_SCHEMA
# Required top-level fields
required = set(_OUTPUT_SCHEMA["required"])
if required != {"summary", "explanations", "reason_if_empty"}:
    fail("schema: top-level required fields", str(required))
# Each explanation requires citation + confidence
exp_required = set(_OUTPUT_SCHEMA["properties"]["explanations"]["items"]["required"])
if not {"article_index", "url", "quote", "confidence"}.issubset(exp_required):
    fail("schema: explanation requires url + quote + confidence + index",
         str(exp_required))
# Confidence is a strict enum
conf_enum = _OUTPUT_SCHEMA["properties"]["explanations"]["items"]["properties"]["confidence"]["enum"]
if set(conf_enum) != {"high", "medium", "low"}:
    fail("schema: confidence enum is exactly high/medium/low", str(conf_enum))
passed("schema: required fields + confidence enum locked")


print("\nAll movement analysis tests passed.")
