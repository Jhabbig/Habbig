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

print("\nAll race_key tests passed.")
