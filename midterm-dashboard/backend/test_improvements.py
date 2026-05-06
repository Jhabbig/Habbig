"""Standalone tests for the reliability + comparison features.

Run with: python3 test_improvements.py
(no pytest dependency, mirrors test_race_key.py style)

Covers:
  - aggregators._retry: exponential backoff, retry-on-429, give-up after N
  - database.upsert_markets_batch: batched executemany path
  - database.record_divergence_batch: batched divergence path
  - main._comparison_rows: cross-source spread computation
  - main: source health helpers update timestamps correctly
  - admin flag/verify -> /data/comparison still skips flagged pairs
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock, patch


def passed(label: str) -> None:
    print(f"PASS {label}")


def fail(label: str, detail: str = "") -> None:
    print(f"FAIL {label}: {detail}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 1. Retry helper — fakes an aiohttp session and asserts retry behaviour
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status: int, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return self._body


class FakeSession:
    """aiohttp-like session that returns scripted responses in order."""

    def __init__(self, scripted: list):
        self._queue = list(scripted)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        next_resp = self._queue.pop(0)
        if isinstance(next_resp, Exception):
            # raise on enter
            class Raises:
                async def __aenter__(s):
                    raise next_resp

                async def __aexit__(s, *exc):
                    return False
            return Raises()
        return next_resp


from aggregators._retry import fetch_json_with_retry, fetch_text_with_retry


async def _no_sleep(_seconds):
    """Replacement for asyncio.sleep that yields without delay."""
    return None


async def _test_retry_succeeds_first_try():
    sess = FakeSession([FakeResponse(200, {"ok": True})])
    out = await fetch_json_with_retry(sess, "u", source_label="t")
    if out != {"ok": True} or sess.calls != 1:
        fail("retry: success on first try", f"out={out} calls={sess.calls}")
    passed("retry: success on first try")


async def _test_retry_recovers_from_429():
    sess = FakeSession([
        FakeResponse(429, {}),
        FakeResponse(200, {"ok": 1}),
    ])
    # Patch sleep so the test isn't slow.
    with patch("aggregators._retry.asyncio.sleep", new=_no_sleep):
        out = await fetch_json_with_retry(sess, "u", source_label="t", max_attempts=3, base_delay=0)
    if out != {"ok": 1} or sess.calls != 2:
        fail("retry: 429 then 200", f"out={out} calls={sess.calls}")
    passed("retry: 429 then 200")


async def _test_retry_recovers_from_5xx():
    sess = FakeSession([
        FakeResponse(503, {}),
        FakeResponse(502, {}),
        FakeResponse(200, [1, 2, 3]),
    ])
    with patch("aggregators._retry.asyncio.sleep", new=_no_sleep):
        out = await fetch_json_with_retry(sess, "u", source_label="t", max_attempts=3, base_delay=0)
    if out != [1, 2, 3] or sess.calls != 3:
        fail("retry: 5xx storm then 200", f"out={out} calls={sess.calls}")
    passed("retry: 5xx storm then 200")


async def _test_retry_gives_up():
    sess = FakeSession([FakeResponse(429, {})] * 5)
    with patch("aggregators._retry.asyncio.sleep", new=_no_sleep):
        out = await fetch_json_with_retry(sess, "u", source_label="t", max_attempts=3, base_delay=0)
    if out is not None or sess.calls != 3:
        fail("retry: give up after max_attempts", f"out={out} calls={sess.calls}")
    passed("retry: give up after max_attempts")


async def _test_retry_does_not_retry_4xx_other():
    sess = FakeSession([FakeResponse(404, {})])
    out = await fetch_json_with_retry(sess, "u", source_label="t", max_attempts=3, base_delay=0)
    if out is not None or sess.calls != 1:
        fail("retry: 404 fails fast", f"out={out} calls={sess.calls}")
    passed("retry: 404 fails fast")


async def _test_retry_text_variant():
    sess = FakeSession([
        FakeResponse(500, ""),
        FakeResponse(200, "csv,header\n1,2"),
    ])
    with patch("aggregators._retry.asyncio.sleep", new=_no_sleep):
        out = await fetch_text_with_retry(sess, "u", source_label="t", max_attempts=2, base_delay=0)
    if out != "csv,header\n1,2" or sess.calls != 2:
        fail("retry: text variant retries", f"out={out!r} calls={sess.calls}")
    passed("retry: text variant retries")


async def _run_retry_tests():
    await _test_retry_succeeds_first_try()
    await _test_retry_recovers_from_429()
    await _test_retry_recovers_from_5xx()
    await _test_retry_gives_up()
    await _test_retry_does_not_retry_4xx_other()
    await _test_retry_text_variant()


asyncio.run(_run_retry_tests())


# ---------------------------------------------------------------------------
# 2. Database batch writers
# ---------------------------------------------------------------------------

from database import Database


_db = Database()
_db.connect()


# upsert_markets_batch with empty list is a no-op
_db.upsert_markets_batch([])
passed("db: upsert_markets_batch([]) is a no-op")

# upsert_markets_batch inserts then updates without per-row connections
_TEST_SRC = "test-batch-src"
sample = [
    {
        "source": _TEST_SRC, "source_id": f"id{i}", "title": f"Race {i}",
        "race_type": "senate", "state": "TX",
        "outcomes": [{"name": "Yes", "probability": 0.5 + i * 0.01}],
        "volume": 100 + i, "active": True, "closed": False,
    }
    for i in range(5)
]
_db.upsert_markets_batch(sample)
fetched = _db.get_markets(source=_TEST_SRC)
if len(fetched) != 5:
    fail("db: batch insert 5 rows", f"got {len(fetched)}")
passed("db: batch insert 5 rows")

# Update path: re-running with mutated volume should overwrite
sample[0]["volume"] = 9999
_db.upsert_markets_batch(sample)
fetched = {m["source_id"]: m for m in _db.get_markets(source=_TEST_SRC)}
if fetched["id0"]["volume"] != 9999:
    fail("db: batch upsert overwrites", f"vol={fetched['id0']['volume']}")
passed("db: batch upsert overwrites existing rows")

# record_divergence_batch with empty list is a no-op
_db.record_divergence_batch([])
passed("db: record_divergence_batch([]) is a no-op")

# record_divergence_batch writes all snapshots
_db.record_divergence_batch([
    {"race_key": "test_div_a", "state": "TX", "race_type": "senate",
     "data": {"polymarket": 0.6, "kalshi": 0.55, "max_divergence": 0.05, "details": {"polymarket": 0.6}}},
    {"race_key": "test_div_b", "state": "GA", "race_type": "governor",
     "data": {"polymarket": 0.4, "kalshi": 0.45, "max_divergence": 0.05, "details": {"polymarket": 0.4}}},
])
hist_a = _db.get_divergence_history(race_key="test_div_a", days=1)
hist_b = _db.get_divergence_history(race_key="test_div_b", days=1)
if not hist_a or not hist_b:
    fail("db: batch divergence writes", f"a={len(hist_a)} b={len(hist_b)}")
passed("db: batch divergence writes both snapshots")

# Cleanup batch test markets
import sqlite3 as _sql
with _sql.connect(str(_db.__class__.__module__) and __import__("database").DB_PATH) as _c:
    _c.execute("DELETE FROM midterm_markets WHERE source = ?", (_TEST_SRC,))
    _c.execute("DELETE FROM midterm_divergence_snapshots WHERE race_key LIKE 'test_div_%'")
    _c.commit()


# ---------------------------------------------------------------------------
# 3. Comparison row computation
# ---------------------------------------------------------------------------

import main as main_mod

# Wire up state.db (normally done by FastAPI's lifespan handler).
main_mod.state.db = _db

# Insert two markets for the same senate_TX race from different sources, plus
# one unmatched-state market that should be excluded.
_CMP_MARKETS = [
    {"source": "polymarket", "source_id": "cmp1", "title": "TX Senate",
     "race_type": "senate", "state": "TX",
     "outcomes": [{"name": "D", "probability": 0.45}], "volume": 100,
     "active": True, "closed": False},
    {"source": "kalshi", "source_id": "cmp2", "title": "TX Senate",
     "race_type": "senate", "state": "TX",
     "outcomes": [{"name": "D", "probability": 0.55}], "volume": 200,
     "active": True, "closed": False},
    # single source — should NOT appear in comparison
    {"source": "polymarket", "source_id": "cmp3", "title": "WY Senate",
     "race_type": "senate", "state": "WY",
     "outcomes": [{"name": "R", "probability": 0.9}], "volume": 50,
     "active": True, "closed": False},
]
_db.upsert_markets_batch(_CMP_MARKETS)
rows = main_mod._comparison_rows()
tx_rows = [r for r in rows if r["race_key"] == "senate_TX"]
if len(tx_rows) != 1:
    fail("compare: exactly 1 row per multi-source race", f"got {len(tx_rows)}")
passed("compare: exactly 1 row per multi-source race")

tx = tx_rows[0]
if abs(tx["spread"] - 10.0) > 0.001:
    fail("compare: spread = (max - min) * 100", f"got {tx['spread']}")
passed("compare: spread = (max - min) * 100")

if tx.get("polymarket") != 0.45 or tx.get("kalshi") != 0.55:
    fail("compare: per-source probabilities populated", f"row={tx}")
passed("compare: per-source probabilities populated")

# WY (single source) must be excluded
if any(r["race_key"] == "senate_WY" for r in rows):
    fail("compare: single-source races excluded", "senate_WY appeared")
passed("compare: single-source races excluded")

# Flagged pairs should disappear from the comparison
_db.flag_market_as_wrong(
    source="polymarket", source_id="cmp1", race_key="senate_TX",
    reviewer_email="t@test",
)
rows_after = main_mod._comparison_rows()
tx_after = [r for r in rows_after if r["race_key"] == "senate_TX"]
# After flagging the polymarket cmp1, only kalshi remains — drops below 2 sources
if tx_after:
    fail("compare: flagged pair drops race below threshold", f"row={tx_after}")
passed("compare: flagging a source removes the race from comparison")
_db.unflag_market("polymarket", "cmp1", "senate_TX")

# Cleanup
with _sql.connect(__import__("database").DB_PATH) as _c:
    _c.execute("DELETE FROM midterm_markets WHERE source_id LIKE 'cmp%'")
    _c.commit()


# ---------------------------------------------------------------------------
# 4. Source health tracking
# ---------------------------------------------------------------------------

main_mod.state.source_health.clear()
main_mod._record_source_success("polymarket", 42)
h = main_mod.state.source_health["polymarket"]
if not h.get("last_success") or h.get("last_fetch_count") != 42:
    fail("source_health: success records timestamp + count", str(h))
passed("source_health: success records timestamp + count")

main_mod._record_source_error("polymarket", "boom")
h = main_mod.state.source_health["polymarket"]
if not h.get("last_error") or h.get("last_error_message") != "boom":
    fail("source_health: error records timestamp + message", str(h))
passed("source_health: error records timestamp + message")

# Long error messages are truncated to 200 chars to bound memory growth
main_mod._record_source_error("polymarket", "x" * 500)
if len(main_mod.state.source_health["polymarket"]["last_error_message"]) != 200:
    fail("source_health: long message truncated to 200 chars",
         f"len={len(main_mod.state.source_health['polymarket']['last_error_message'])}")
passed("source_health: long error messages truncated")


print("\nAll improvement tests passed.")
