"""Tests for cross-book arbitrage scanners: low-hold and middles."""
import sports_dashboard as sd


def _make_event(market_type, bookmakers, home="Lakers", away="Warriors"):
    """Build a parsed-events row in the shape produced by parse_odds_events."""
    return {
        "id": "evt1",
        "home_team": home,
        "away_team": away,
        "commence_time": "2026-01-15T20:00:00Z",
        "market_type": market_type,
        "bookmakers": bookmakers,
        "sharp_book": list(bookmakers.keys())[0] if bookmakers else None,
        "sharp_outcomes": {},
        "consensus_probs": {},
        "num_bookmakers": len(bookmakers),
    }


# ── Low-hold scanner ────────────────────────────────────────────────────────

def test_low_hold_no_arb_when_sum_above_100():
    """Two-book h2h with 5% combined vig: not an arb."""
    event = _make_event("h2h", {
        "draftkings": {"title": "DK", "outcomes": {
            "Lakers": {"implied_prob": 55.0, "decimal_odds": 1.82},
            "Warriors": {"implied_prob": 50.0, "decimal_odds": 2.00},
        }},
        "fanduel": {"title": "FD", "outcomes": {
            "Lakers": {"implied_prob": 53.0, "decimal_odds": 1.89},
            "Warriors": {"implied_prob": 52.0, "decimal_odds": 1.92},
        }},
    })
    rows = sd.find_low_hold_opportunities([event])
    assert rows == []


def test_low_hold_detects_negative_vig():
    """DK's Lakers price + FD's Warriors price sum to 99% → arb."""
    event = _make_event("h2h", {
        "draftkings": {"title": "DK", "outcomes": {
            "Lakers": {"implied_prob": 45.0, "decimal_odds": 2.22},
            "Warriors": {"implied_prob": 60.0, "decimal_odds": 1.67},
        }},
        "fanduel": {"title": "FD", "outcomes": {
            "Lakers": {"implied_prob": 60.0, "decimal_odds": 1.67},
            "Warriors": {"implied_prob": 54.0, "decimal_odds": 1.85},
        }},
    })
    # Best Lakers = 45 (DK), best Warriors = 54 (FD) → sum 99
    rows = sd.find_low_hold_opportunities([event])
    assert len(rows) == 1
    r = rows[0]
    assert r["total_implied_pp"] == 99.0
    assert r["gap_pp"] == 1.0
    assert r["profit_pct"] > 0
    legs_by_outcome = {leg["outcome"]: leg for leg in r["legs"]}
    assert legs_by_outcome["Lakers"]["book"] == "draftkings"
    assert legs_by_outcome["Warriors"]["book"] == "fanduel"


def test_low_hold_uses_lowest_implied_per_outcome():
    """When 3+ books quote an outcome, we pick the lowest implied prob
    (best price for the bettor)."""
    event = _make_event("h2h", {
        "dk": {"title": "DK", "outcomes": {
            "Lakers": {"implied_prob": 50.0, "decimal_odds": 2.00},
            "Warriors": {"implied_prob": 55.0, "decimal_odds": 1.82},
        }},
        "fd": {"title": "FD", "outcomes": {
            "Lakers": {"implied_prob": 48.0, "decimal_odds": 2.08},
            "Warriors": {"implied_prob": 58.0, "decimal_odds": 1.72},
        }},
        "bm": {"title": "BetMGM", "outcomes": {
            "Lakers": {"implied_prob": 47.0, "decimal_odds": 2.13},  # best Lakers
            "Warriors": {"implied_prob": 52.0, "decimal_odds": 1.92},  # best Warriors
        }},
    })
    rows = sd.find_low_hold_opportunities([event])
    assert len(rows) == 1
    legs = {leg["outcome"]: leg for leg in rows[0]["legs"]}
    # BetMGM should appear on both legs since it has the best price for both
    assert legs["Lakers"]["book"] == "bm"
    assert legs["Warriors"]["book"] == "bm"


def test_low_hold_skips_events_with_only_one_outcome():
    event = _make_event("h2h", {
        "dk": {"title": "DK", "outcomes": {
            "Lakers": {"implied_prob": 80.0, "decimal_odds": 1.25},
        }},
    })
    assert sd.find_low_hold_opportunities([event]) == []


def test_low_hold_sorts_by_gap_desc():
    big = _make_event("h2h", {
        "dk": {"title": "DK", "outcomes": {
            "A": {"implied_prob": 40.0, "decimal_odds": 2.5},
            "B": {"implied_prob": 50.0, "decimal_odds": 2.0},
        }},
        "fd": {"title": "FD", "outcomes": {
            "A": {"implied_prob": 50.0, "decimal_odds": 2.0},
            "B": {"implied_prob": 48.0, "decimal_odds": 2.08},  # best B
        }},
    }, home="X", away="Y")
    small = _make_event("h2h", {
        "dk": {"title": "DK", "outcomes": {
            "A": {"implied_prob": 49.0, "decimal_odds": 2.04},
            "B": {"implied_prob": 50.0, "decimal_odds": 2.0},
        }},
        "fd": {"title": "FD", "outcomes": {
            "A": {"implied_prob": 50.0, "decimal_odds": 2.0},
            "B": {"implied_prob": 50.5, "decimal_odds": 1.98},
        }},
    }, home="C", away="D")
    rows = sd.find_low_hold_opportunities([small, big])
    # Big gap (12pp) should come first
    assert rows[0]["gap_pp"] > rows[1]["gap_pp"]


# ── Middle scanner ──────────────────────────────────────────────────────────

def test_middle_totals_detects_basic_middle():
    """DK total Over 220.5 + FD total Under 222.5 → middle of 2 points
    (final score lands in 221 or 222 wins both bets)."""
    event = _make_event("totals", {
        "dk_totals": {"title": "DK", "outcomes": {
            "Over 220.5": {"implied_prob": 50.0, "point": 220.5},
            "Under 220.5": {"implied_prob": 52.0, "point": 220.5},
        }},
        "fd_totals": {"title": "FD", "outcomes": {
            "Over 222.5": {"implied_prob": 53.0, "point": 222.5},
            "Under 222.5": {"implied_prob": 49.0, "point": 222.5},
        }},
    })
    rows = sd.find_middle_opportunities([event])
    # Expect at least one middle: DK Over 220.5 + FD Under 222.5
    middles = [r for r in rows
               if r["over_leg"]["line"] == 220.5 and r["under_leg"]["line"] == 222.5]
    assert len(middles) == 1
    m = middles[0]
    assert m["middle_width"] == 2.0
    assert m["over_leg"]["book"] == "dk"
    assert m["under_leg"]["book"] == "fd"


def test_middle_totals_skips_same_book():
    """If only one book is involved we can't middle — both quotes come
    from the same shop."""
    event = _make_event("totals", {
        "dk_totals": {"title": "DK", "outcomes": {
            "Over 220.5": {"implied_prob": 50.0, "point": 220.5},
            "Under 222.5": {"implied_prob": 50.0, "point": 222.5},
        }},
    })
    assert sd.find_middle_opportunities([event]) == []


def test_middle_totals_skips_when_lines_not_a_middle():
    """Over 222.5 and Under 220.5 is *anti*-middle — both legs lose if
    the score lands between."""
    event = _make_event("totals", {
        "dk_totals": {"title": "DK", "outcomes": {
            "Over 222.5": {"implied_prob": 50.0, "point": 222.5},
            "Under 222.5": {"implied_prob": 52.0, "point": 222.5},
        }},
        "fd_totals": {"title": "FD", "outcomes": {
            "Over 220.5": {"implied_prob": 53.0, "point": 220.5},
            "Under 220.5": {"implied_prob": 49.0, "point": 220.5},
        }},
    })
    rows = sd.find_middle_opportunities([event])
    # No row should have over.line >= under.line
    for r in rows:
        assert r["over_leg"]["line"] < r["under_leg"]["line"]


def test_middle_spreads_detects_team_middle():
    """Underdog +4.5 at DK + favorite -2.5 at FD → 2-point middle on the
    favorite winning by 3 or 4 (covers both)."""
    event = _make_event("spreads", {
        "dk_spreads": {"title": "DK", "outcomes": {
            "Warriors +4.5": {"implied_prob": 50.0, "point": 4.5},
            "Lakers -4.5": {"implied_prob": 52.0, "point": -4.5},
        }},
        "fd_spreads": {"title": "FD", "outcomes": {
            "Warriors +2.5": {"implied_prob": 53.0, "point": 2.5},
            "Lakers -2.5": {"implied_prob": 49.0, "point": -2.5},
        }},
    })
    rows = sd.find_middle_opportunities([event])
    # Find Warriors +4.5 (DK) middled against Lakers -2.5 (FD)
    middles = [r for r in rows
               if r["over_leg"]["outcome"] == "Warriors +4.5"
               and r["under_leg"]["outcome"] == "Lakers -2.5"]
    assert len(middles) == 1
    assert middles[0]["middle_width"] == 2.0


def test_middle_ignores_h2h():
    event = _make_event("h2h", {
        "dk": {"title": "DK", "outcomes": {
            "Lakers": {"implied_prob": 55.0},
            "Warriors": {"implied_prob": 50.0},
        }},
    })
    assert sd.find_middle_opportunities([event]) == []


def test_middle_sorted_by_width_then_cost():
    """Wider middles come first; ties broken by lower cost."""
    wide = _make_event("totals", {
        "dk_totals": {"title": "DK", "outcomes": {
            "Over 215.5": {"implied_prob": 50.0, "point": 215.5},
            "Under 215.5": {"implied_prob": 51.0, "point": 215.5},
        }},
        "fd_totals": {"title": "FD", "outcomes": {
            "Over 220.5": {"implied_prob": 51.0, "point": 220.5},
            "Under 220.5": {"implied_prob": 50.0, "point": 220.5},
        }},
    }, home="A", away="B")
    narrow = _make_event("totals", {
        "dk_totals": {"title": "DK", "outcomes": {
            "Over 220.5": {"implied_prob": 50.0, "point": 220.5},
            "Under 220.5": {"implied_prob": 51.0, "point": 220.5},
        }},
        "fd_totals": {"title": "FD", "outcomes": {
            "Over 221.5": {"implied_prob": 51.0, "point": 221.5},
            "Under 221.5": {"implied_prob": 50.0, "point": 221.5},
        }},
    }, home="C", away="D")
    rows = sd.find_middle_opportunities([narrow, wide])
    assert rows[0]["middle_width"] >= rows[-1]["middle_width"]
