"""Tests for market_race_key.

Run with: ./venv/bin/python test_race_key.py
(no pytest dependency)

Guards the regression: prior to the fix, both Bulgarian elections and
"Will LeBron James be the next president" collapsed into the same
``other_US`` race_key bucket and the divergence calculator grouped them
as the same race across sources.
"""
from __future__ import annotations

import sys

from main import market_race_key


def assert_eq(actual, expected, label):
    if actual != expected:
        print(f"FAIL {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"PASS {label}")


def assert_ne(actual, other, label):
    if actual == other:
        print(f"FAIL {label}: both equal {actual!r}")
        sys.exit(1)
    print(f"PASS {label}")


def assert_starts(actual, prefix, label):
    if not actual.startswith(prefix):
        print(f"FAIL {label}: {actual!r} does not start with {prefix!r}")
        sys.exit(1)
    print(f"PASS {label}")


# --- Real races still produce stable canonical keys -------------------------
assert_eq(
    market_race_key({"race_type": "senate", "state": "GA", "source": "polymarket", "source_id": "1"}),
    "senate_GA",
    "senate_GA stable",
)
assert_eq(
    market_race_key({"race_type": "governor", "state": "PA", "source": "kalshi", "source_id": "2"}),
    "governor_PA",
    "governor_PA stable",
)
assert_eq(
    market_race_key({"race_type": "world", "state": "HU", "source": "polymarket", "source_id": "3"}),
    "world_HU",
    "world_HU stable",
)

# Cross-source same race shares the key
poly_senate = {"race_type": "senate", "state": "GA", "source": "polymarket", "source_id": "p1"}
kalshi_senate = {"race_type": "senate", "state": "GA", "source": "kalshi", "source_id": "k1"}
assert_eq(
    market_race_key(poly_senate), market_race_key(kalshi_senate),
    "polymarket and kalshi senate_GA share key",
)

# House district parsing
assert_eq(
    market_race_key({
        "race_type": "house",
        "state": "TX",
        "title": "Texas's 28th Congressional District",
        "source": "polymarket",
        "source_id": "h1",
    }),
    "house_TX-28",
    "house TX-28 parsed",
)

# House without parseable district → unmatched (do NOT collapse to state-level)
house_unparseable = market_race_key({
    "race_type": "house",
    "state": "TX",
    "title": "Some unrelated headline",
    "source": "polymarket",
    "source_id": "h2",
})
assert_starts(house_unparseable, "unmatched_", "unparseable house → unmatched")

# --- The regression: unrelated markets must NOT share a key -----------------
lebron = {
    "race_type": "other",
    "state": None,
    "title": "Will LeBron James be the next president",
    "source": "polymarket",
    "source_id": "lebron-id",
}
bulgaria = {
    "race_type": "other",
    "state": None,
    "title": "Bulgarian parliamentary elections 2026",
    "source": "polymarket",
    "source_id": "bulgaria-id",
}
lebron_key = market_race_key(lebron)
bulgaria_key = market_race_key(bulgaria)
assert_starts(lebron_key, "unmatched_", "LeBron isolated")
assert_starts(bulgaria_key, "unmatched_", "Bulgaria isolated")
assert_ne(lebron_key, bulgaria_key, "LeBron and Bulgaria do not collide")

# Even with the same source, source_id makes them distinct
poly_a = {"race_type": "other", "state": None, "source": "polymarket", "source_id": "a"}
poly_b = {"race_type": "other", "state": None, "source": "polymarket", "source_id": "b"}
assert_ne(
    market_race_key(poly_a), market_race_key(poly_b),
    "two unmatched polymarket markets stay distinct",
)

# Missing race_type entirely → unmatched
no_rt = {"state": "GA", "source": "polymarket", "source_id": "x"}
assert_starts(market_race_key(no_rt), "unmatched_", "missing race_type → unmatched")

# Missing state entirely → unmatched
no_state = {"race_type": "senate", "source": "polymarket", "source_id": "y"}
assert_starts(market_race_key(no_state), "unmatched_", "missing state → unmatched")


# =============================================================================
# Human-review flag/verify round-trip tests
# =============================================================================
# These exercise the real DB — we use a throwaway race_key/source_id prefix
# so we don't collide with any real data that may exist in data.db.

from database import Database

_db = Database()
_db.connect()

_TEST_RACE = "test_race_key_review_xyz"
_TEST_SRC = "polymarket"
_TEST_ID = "test-market-review-xyz"

# Clean slate in case a previous failed run left rows behind
_db.unflag_market(_TEST_SRC, _TEST_ID, _TEST_RACE)
_db.unverify_race(_TEST_RACE)

# Flag round-trip ------------------------------------------------------------
_db.flag_market_as_wrong(
    source=_TEST_SRC, source_id=_TEST_ID, race_key=_TEST_RACE,
    reviewer_email="reviewer@test", note="smoke test",
)
flags = _db.get_flags_for_race(_TEST_RACE)
assert any(f["source_id"] == _TEST_ID for f in flags), "flag not persisted"
print("PASS flag persisted via get_flags_for_race")

all_flags = _db.get_all_wrong_flags()
assert (_TEST_SRC, _TEST_ID) in all_flags.get(_TEST_RACE, set()), "flag missing from get_all_wrong_flags"
print("PASS flag visible in get_all_wrong_flags")

# Idempotency — flagging twice should not create duplicates
_db.flag_market_as_wrong(
    source=_TEST_SRC, source_id=_TEST_ID, race_key=_TEST_RACE,
    reviewer_email="reviewer@test", note="second call",
)
flags2 = _db.get_flags_for_race(_TEST_RACE)
our_rows = [f for f in flags2 if f["source_id"] == _TEST_ID]
assert len(our_rows) == 1, f"expected 1 flag row after idempotent call, got {len(our_rows)}"
assert our_rows[0]["note"] == "second call", "note should have been updated"
print("PASS flag upsert is idempotent")

# Unflag
removed = _db.unflag_market(_TEST_SRC, _TEST_ID, _TEST_RACE)
assert removed, "unflag should report row removed"
flags3 = _db.get_flags_for_race(_TEST_RACE)
assert not any(f["source_id"] == _TEST_ID for f in flags3), "flag should be gone"
print("PASS unflag removes the row")

# Unflagging a nonexistent row returns False (not an error)
assert _db.unflag_market(_TEST_SRC, _TEST_ID, _TEST_RACE) is False
print("PASS unflag on missing row returns False")

# Verify round-trip ----------------------------------------------------------
_db.verify_race(_TEST_RACE, reviewer_email="reviewer@test", note="looks good")
v = _db.get_race_verification(_TEST_RACE)
assert v is not None and v["reviewer_email"] == "reviewer@test"
print("PASS verify_race persists")

# Idempotent
_db.verify_race(_TEST_RACE, reviewer_email="other@test", note="updated")
v2 = _db.get_race_verification(_TEST_RACE)
assert v2["reviewer_email"] == "other@test" and v2["note"] == "updated"
print("PASS verify_race upsert updates existing row")

# Unverify
removed_v = _db.unverify_race(_TEST_RACE)
assert removed_v is True
assert _db.get_race_verification(_TEST_RACE) is None
print("PASS unverify_race removes the row")

# Unverifying a nonexistent race returns False
assert _db.unverify_race(_TEST_RACE) is False
print("PASS unverify on missing row returns False")

# Flag filtering simulates the divergence_calculator skip -------------------
# Add a flag, then check get_all_wrong_flags gives us the tuple we'd use
# in the real grouping loop.
_db.flag_market_as_wrong(
    source=_TEST_SRC, source_id=_TEST_ID, race_key=_TEST_RACE,
    reviewer_email="reviewer@test",
)
flags_map = _db.get_all_wrong_flags()
fake_market = {"source": _TEST_SRC, "source_id": _TEST_ID}
is_flagged = (fake_market["source"], fake_market["source_id"]) in flags_map.get(_TEST_RACE, set())
assert is_flagged, "divergence_calculator filter would not skip this market"
print("PASS divergence_calculator skip condition triggers for flagged market")

# Cleanup
_db.unflag_market(_TEST_SRC, _TEST_ID, _TEST_RACE)

print("\nAll race_key + review tests passed.")
