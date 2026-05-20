"""Tests for the canonical-question matcher and party-classifier.

These two functions are the highest-leverage things in the dashboard's data
pipeline: ``_canonical_question`` decides which markets are compared against
each other for divergence; ``_classify_outcome_party`` decides how Yes/No
outcomes contribute to Senate/House control probabilities. A mistake in
either silently corrupts the user-facing odds.

Run:
    cd backend && python3 test_canonical_question.py
"""
from __future__ import annotations

import sys

from main import _canonical_question, _classify_outcome_party


def _expect(actual, expected, label):
    if actual != expected:
        print(f"FAIL {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"PASS {label}")


def test_canonical_question_state_winner():
    m = {
        "title": "Who will win the 2026 Texas Senate election?",
        "race_type": "senate",
        "state": "TX",
    }
    _expect(_canonical_question(m), "senate_TX_winner", "state_winner")


def test_canonical_question_party_winner():
    m = {
        "title": "Will Democrats win the 2026 Texas Senate election?",
        "race_type": "senate",
        "state": "TX",
    }
    _expect(_canonical_question(m), "senate_TX_party_winner", "party_winner")


def test_canonical_question_primary_d_vs_r():
    dem = {
        "title": "2026 Texas Democratic Senate primary nominee",
        "race_type": "senate",
        "state": "TX",
    }
    rep = {
        "title": "2026 Texas Republican Senate primary nominee",
        "race_type": "senate",
        "state": "TX",
    }
    a = _canonical_question(dem)
    b = _canonical_question(rep)
    if a == b:
        print(f"FAIL primary_d_vs_r: D and R primaries collapsed into {a!r}")
        sys.exit(1)
    print(f"PASS primary_d_vs_r ({a!r} != {b!r})")


def test_canonical_question_national_senate_control():
    m = {"title": "Will Republicans control the Senate in 2026?", "race_type": "control", "state": None}
    _expect(_canonical_question(m), "national_senate_control", "national_senate_control")


def test_canonical_question_national_house_control():
    m = {"title": "Will Democrats control the House in 2026?", "race_type": "control", "state": None}
    _expect(_canonical_question(m), "national_house_control", "national_house_control")


def test_canonical_question_senate_seats_count():
    m = {"title": "Will Republicans hold exactly 53 Senate seats?", "race_type": "control", "state": None}
    _expect(_canonical_question(m), "national_senate_seats", "national_senate_seats")


def test_canonical_question_geo_does_not_collide():
    """Bulgarian election and Will-LeBron-be-president must not share a key."""
    a = _canonical_question({"title": "Will China invade Taiwan by end of 2026?", "race_type": "other", "state": None, "source": "polymarket", "source_id": "abc"})
    b = _canonical_question({"title": "Will LeBron James be next president?", "race_type": "other", "state": None, "source": "polymarket", "source_id": "xyz"})
    if a == b:
        print(f"FAIL geo_does_not_collide: {a!r} == {b!r}")
        sys.exit(1)
    print(f"PASS geo_does_not_collide")


def test_classify_party_named():
    _expect(_classify_outcome_party("Democratic", "Senate control 2026"), "democrat", "named_dem")
    _expect(_classify_outcome_party("Republican", "Senate control 2026"), "republican", "named_rep")
    _expect(_classify_outcome_party("GOP", "Senate control 2026"), "republican", "named_gop")
    _expect(_classify_outcome_party("Dem", "Senate control 2026"), "democrat", "named_dem_short")


def test_classify_party_yes_no():
    title_d = "Will Democrats win the Senate?"
    title_r = "Will Republicans hold the House?"
    _expect(_classify_outcome_party("Yes", title_d), "democrat", "yes_dem")
    _expect(_classify_outcome_party("No", title_d), "republican", "no_dem")
    _expect(_classify_outcome_party("Yes", title_r), "republican", "yes_rep")
    _expect(_classify_outcome_party("No", title_r), "democrat", "no_rep")


def test_classify_party_unclassifiable():
    _expect(_classify_outcome_party("Tie", "Senate split 50-50?"), None, "unclassifiable_tie")
    _expect(_classify_outcome_party("Yes", "Will the moon turn green?"), None, "unclassifiable_yes")


def test_polymarket_state_extraction():
    """Hardcoded state extraction was replaced with a shared lookup; sanity-test
    the dashboard's wiring."""
    from aggregators.polymarket import PolymarketAggregator

    _expect(PolymarketAggregator._extract_state("2026 Senate Race in Texas"), "TX", "extract_TX")
    _expect(PolymarketAggregator._extract_state("Pennsylvania governor 2026"), "PA", "extract_PA")
    # Washington D.C. must NOT match Washington state
    _expect(PolymarketAggregator._extract_state("Will Washington D.C. become a state?"), None, "extract_no_DC")


def test_polymarket_country_extraction():
    from aggregators.polymarket import PolymarketAggregator

    _expect(PolymarketAggregator._extract_country("UK general election 2026"), "UK", "country_UK")
    _expect(PolymarketAggregator._extract_country("French presidential"), "FR", "country_FR")
    _expect(PolymarketAggregator._extract_country("Ukrainian presidential election"), "UA", "country_UA_not_UK")


if __name__ == "__main__":
    test_canonical_question_state_winner()
    test_canonical_question_party_winner()
    test_canonical_question_primary_d_vs_r()
    test_canonical_question_national_senate_control()
    test_canonical_question_national_house_control()
    test_canonical_question_senate_seats_count()
    test_canonical_question_geo_does_not_collide()
    test_classify_party_named()
    test_classify_party_yes_no()
    test_classify_party_unclassifiable()
    test_polymarket_state_extraction()
    test_polymarket_country_extraction()
    print("\nAll canonical-question tests passed.")
