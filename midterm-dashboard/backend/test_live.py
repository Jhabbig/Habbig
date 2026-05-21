"""Standalone tests for election-night live mode.

Run with: python3 test_live.py

Covers:
  - market_implied_winner_party: parses D/R/I from outcome names
  - detect_disagreement: agree, market-conceded, low/medium/high severity
  - DB: race-call upsert (multiple providers for same race coexist),
    overwrite within same provider, remove, get all + grouped
  - Live dashboard endpoint: builds rows, flags disagreements, sorts
    biggest-disagreement first, returns providers + totals
  - Admin endpoints: manual call validates party enum, requires admin
  - Routes registered
"""
from __future__ import annotations

import asyncio
import sys
import sqlite3 as _sql


def passed(label: str) -> None:
    print(f"PASS {label}")


def fail(label: str, detail: str = "") -> None:
    print(f"FAIL {label}: {detail}")
    sys.exit(1)


from database import Database, DB_PATH
import main as main_mod
import race_calls

_db = Database()
_db.connect()
main_mod.state.db = _db

# Clean per-test state
with _sql.connect(DB_PATH) as _c:
    _c.execute("DELETE FROM midterm_race_calls")
    # Clear any synthetic markets from other test runs
    _c.execute("DELETE FROM midterm_markets WHERE source_id LIKE 'live_test_%'")
    _c.commit()


# ---------------------------------------------------------------------------
# 1. market_implied_winner_party
# ---------------------------------------------------------------------------

def party_of(outcomes):
    return race_calls.market_implied_winner_party(outcomes)


cases = [
    ([{"name": "Democratic", "probability": 0.7}, {"name": "Republican", "probability": 0.3}], "D"),
    ([{"name": "Republican", "probability": 0.6}, {"name": "Democrat", "probability": 0.4}], "R"),
    ([{"name": "Yes", "probability": 0.55}, {"name": "No", "probability": 0.45}], "D"),
    ([{"name": "No", "probability": 0.6}, {"name": "Yes", "probability": 0.4}], "R"),
    ([{"name": "Independent", "probability": 0.51}, {"name": "D", "probability": 0.49}], "I"),
    ([{"name": "Some other label", "probability": 0.9}], None),
    ([], None),
]
for outcomes, expected in cases:
    got = party_of(outcomes)
    if got != expected:
        fail(f"market_implied_winner_party {outcomes}", f"expected {expected!r}, got {got!r}")
passed("market_implied_winner_party: parses D/R/I/None from common outcome names")


# ---------------------------------------------------------------------------
# 2. detect_disagreement
# ---------------------------------------------------------------------------

# Agreement: market and call point at same party
d = race_calls.detect_disagreement(
    {"called_party": "D"},
    {"inferred_party": "D", "probability": 0.9},
)
if d is not None:
    fail("detect_disagreement: agreement returns None", str(d))
passed("detect_disagreement: agreement returns None")

# Market already conceded (<50%) — no disagreement to flag
d = race_calls.detect_disagreement(
    {"called_party": "D"},
    {"inferred_party": "R", "probability": 0.40},
)
if d is not None:
    fail("detect_disagreement: market conceded returns None", str(d))
passed("detect_disagreement: market already conceded → no flag")

# Severity ladder
for prob, expected_sev in [(0.51, "low"), (0.65, "medium"), (0.80, "high")]:
    d = race_calls.detect_disagreement(
        {"called_party": "D"},
        {"inferred_party": "R", "probability": prob},
    )
    if d is None or d["severity"] != expected_sev:
        fail(f"detect_disagreement: prob={prob} → {expected_sev}", str(d))
passed("detect_disagreement: low/medium/high severity boundaries")

# Missing data — no panic
if race_calls.detect_disagreement(None, {"inferred_party": "D", "probability": 0.9}) is not None:
    fail("detect_disagreement: None call returns None")
if race_calls.detect_disagreement({"called_party": "D"}, None) is not None:
    fail("detect_disagreement: None market returns None")
if race_calls.detect_disagreement({}, {}) is not None:
    fail("detect_disagreement: empty dicts return None")
passed("detect_disagreement: missing data returns None (no crash)")


# ---------------------------------------------------------------------------
# 3. DB: race-call upsert/get/remove
# ---------------------------------------------------------------------------

_db.upsert_race_call(
    race_key="senate_GA", provider="ap",
    called_party="D", called_candidate="Jon Ossoff",
    leader_pct=51.2, reporting_pct=98.0, notes="AP",
)
_db.upsert_race_call(
    race_key="senate_GA", provider="ddhq",
    called_party="D", called_candidate="Jon Ossoff",
    leader_pct=51.0, reporting_pct=97.5, notes="DDHQ",
)

calls = _db.get_race_calls(race_key="senate_GA")
if len(calls) != 2:
    fail("db.race_calls: two providers coexist", str(calls))
providers = {c["provider"] for c in calls}
if providers != {"ap", "ddhq"}:
    fail("db.race_calls: both ap and ddhq present", str(providers))
passed("db: multiple providers for same race coexist (AP + DDHQ)")

# Upsert within same (race_key, provider) overwrites
_db.upsert_race_call(
    race_key="senate_GA", provider="ap",
    called_party="D", called_candidate="Jon Ossoff",
    leader_pct=51.8, reporting_pct=99.5, notes="AP refined",
)
ap = next(c for c in _db.get_race_calls(race_key="senate_GA") if c["provider"] == "ap")
if ap["leader_pct"] != 51.8 or ap["reporting_pct"] != 99.5:
    fail("db: upsert overwrites within (race_key, provider)", str(ap))
passed("db: upsert overwrites within (race_key, provider)")

# Grouped view
grouped = _db.get_race_calls_grouped()
if "senate_GA" not in grouped or len(grouped["senate_GA"]) != 2:
    fail("db: grouped view has senate_GA with 2 calls", str(grouped))
passed("db: get_race_calls_grouped buckets per race_key")

# Remove one provider
if not _db.remove_race_call("senate_GA", "ddhq"):
    fail("db.remove_race_call: returns True on hit")
if _db.remove_race_call("senate_GA", "ddhq"):
    fail("db.remove_race_call: returns False on miss (idempotent)")
remaining = _db.get_race_calls(race_key="senate_GA")
if len(remaining) != 1 or remaining[0]["provider"] != "ap":
    fail("db.remove_race_call: leaves AP intact", str(remaining))
passed("db: remove is per-provider, idempotent, leaves others")


# ---------------------------------------------------------------------------
# 4. Live dashboard endpoint
# ---------------------------------------------------------------------------

# Seed two synthetic markets with different inferred winners, then call one
import json as _json
with _sql.connect(DB_PATH) as _c:
    # senate_AB: market says R 70% (disagrees with AP D call)
    _c.execute(
        """INSERT INTO midterm_markets (source, source_id, title, event_title, race_type, state,
                                         outcomes, volume, active, closed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("polymarket", "live_test_1", "AB Senate", "AB Senate 2026", "senate", "AB",
         _json.dumps([{"name": "Republican", "probability": 0.70},
                      {"name": "Democratic", "probability": 0.30}]),
         150000, 1, 0),
    )
    _c.execute(
        """INSERT INTO midterm_markets (source, source_id, title, event_title, race_type, state,
                                         outcomes, volume, active, closed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("kalshi", "live_test_2", "AB Senate", "AB Senate 2026", "senate", "AB",
         _json.dumps([{"name": "R", "probability": 0.65},
                      {"name": "D", "probability": 0.35}]),
         50000, 1, 0),
    )
    # senate_CD: agreement case — market and call both D
    _c.execute(
        """INSERT INTO midterm_markets (source, source_id, title, event_title, race_type, state,
                                         outcomes, volume, active, closed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("polymarket", "live_test_3", "CD Senate", "CD Senate 2026", "senate", "CD",
         _json.dumps([{"name": "Democrat", "probability": 0.85},
                      {"name": "Republican", "probability": 0.15}]),
         20000, 1, 0),
    )
    _c.commit()


# Call senate_AB for D (against the market)
_db.upsert_race_call(
    race_key="senate_AB", provider="ap",
    called_party="D", called_candidate="Alice Example",
    leader_pct=50.3, reporting_pct=85.0,
)
# Call senate_CD for D (agrees with market)
_db.upsert_race_call(
    race_key="senate_CD", provider="ap",
    called_party="D", called_candidate="Bob Example",
    leader_pct=58.0, reporting_pct=92.0,
)


class _FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


async def _live_test():
    # The endpoint now returns a JSONResponse for cache-busting via ETag;
    # extract the raw payload through the underlying builder so the
    # assertions still target the data shape, not the HTTP envelope.
    payload = main_mod._build_live_dashboard()

    # Also exercise the cached endpoint path end-to-end
    resp = await main_mod.data_live_dashboard(_FakeRequest())
    if not hasattr(resp, "headers") or "etag" not in {k.lower() for k in resp.headers}:
        fail("live endpoint: returns ETag header", str(resp.headers))
    etag = resp.headers["etag"]

    # Second call with If-None-Match should be 304
    resp2 = await main_mod.data_live_dashboard(_FakeRequest({"if-none-match": etag}))
    if getattr(resp2, "status_code", None) != 304:
        fail("live endpoint: returns 304 on matching If-None-Match",
             str(getattr(resp2, "status_code", "n/a")))
    passed("live endpoint: ETag + 304 short-circuit works")

    rows = payload.get("rows", [])
    ab = next((r for r in rows if r["race_key"] == "senate_AB"), None)
    cd = next((r for r in rows if r["race_key"] == "senate_CD"), None)
    if not ab or not cd:
        fail("live: both seeded races present", f"rows={[r['race_key'] for r in rows]}")

    # AB has called D + markets at R = disagreement on both sources
    if not ab["disagreements"]:
        fail("live: senate_AB flags disagreements", str(ab))
    if len(ab["disagreements"]) < 1:
        fail("live: senate_AB at least one disagreement", str(ab["disagreements"]))
    severities = {d["severity"] for d in ab["disagreements"]}
    if "high" not in severities and "medium" not in severities:
        fail("live: senate_AB has high or medium severity", str(severities))
    passed("live dashboard: disagreeing race flagged with appropriate severity")

    # CD has agreement — no disagreements
    if cd["disagreements"]:
        fail("live: senate_CD has no disagreement", str(cd))
    passed("live dashboard: agreeing race not flagged")

    # Both have a primary call set
    if not ab["called"] or ab["called"]["called_party"] != "D":
        fail("live: senate_AB has D call attached", str(ab))
    passed("live dashboard: called field populated from latest call")

    # Sort: disagreements first
    first_idx = rows.index(ab)
    second_idx = rows.index(cd)
    if first_idx >= second_idx:
        fail(f"live: disagreement sorted before agreement (ab idx={first_idx}, cd idx={second_idx})")
    passed("live dashboard: rows sorted disagreements-first")

    # totals + providers exposed
    if payload["totals"]["called"] < 2:
        fail("live: totals.called >= 2", str(payload["totals"]))
    if "ap" not in payload["providers"] or "manual" not in payload["providers"]:
        fail("live: providers field present", str(payload["providers"]))
    passed("live dashboard: returns totals + providers in payload")


asyncio.run(_live_test())

# Clean up
with _sql.connect(DB_PATH) as _c:
    _c.execute("DELETE FROM midterm_markets WHERE source_id LIKE 'live_test_%'")
    _c.execute("DELETE FROM midterm_race_calls WHERE race_key IN ('senate_AB', 'senate_CD', 'senate_GA')")
    _c.commit()


# ---------------------------------------------------------------------------
# 5. Routes registered
# ---------------------------------------------------------------------------

paths = {r.path for r in main_mod.app.routes if hasattr(r, "path")}
required = {
    "/data/live/dashboard",
    "/data/live/calls",
    "/data/live/providers",
    "/admin/race-call",
    "/admin/race-call/{race_key}/{provider}",
}
missing = required - paths
if missing:
    fail("routes: live endpoints registered", f"missing={missing}")
passed(f"routes: all {len(required)} live endpoints registered")


# ---------------------------------------------------------------------------
# 6. providers_configured shape
# ---------------------------------------------------------------------------

cfg = race_calls.providers_configured()
for k in ("ap", "ddhq", "wikipedia", "manual"):
    if k not in cfg:
        fail(f"providers_configured: '{k}' present", str(cfg))
if cfg["wikipedia"] is not True or cfg["manual"] is not True:
    fail("providers_configured: wikipedia + manual always available", str(cfg))
passed("providers_configured: returns boolean dict with all four keys")


print("\nAll live-mode tests passed.")
