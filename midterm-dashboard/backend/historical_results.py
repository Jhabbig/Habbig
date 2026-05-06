from __future__ import annotations
"""Historical US election results.

Hand-curated dataset of recent federal/statewide election winners including
vote totals and margins. Used by the /data/historical endpoint so users can
compare current prediction markets against historical baselines.

Sources: state Secretaries of State, FEC, and Cook Political Report archives.
"""

# Schema: (year, race_type, state, winner, party, winner_pct, runner_up,
#          runner_up_party, runner_up_pct, winner_votes, runner_up_votes, margin_pct)

HISTORICAL_RESULTS = [
    # 2024 Presidential
    {"year": 2024, "race_type": "president", "state": "US", "winner": "Donald Trump", "party": "R",
     "winner_pct": 49.8, "runner_up": "Kamala Harris", "runner_up_party": "D",
     "runner_up_pct": 48.3, "winner_votes": 77303573, "runner_up_votes": 75019257, "margin_pct": 1.5},

    # 2024 Senate (key races)
    {"year": 2024, "race_type": "senate", "state": "OH", "winner": "Bernie Moreno", "party": "R",
     "winner_pct": 50.1, "runner_up": "Sherrod Brown", "runner_up_party": "D",
     "runner_up_pct": 46.5, "winner_votes": 3080404, "runner_up_votes": 2858264, "margin_pct": 3.6},
    {"year": 2024, "race_type": "senate", "state": "MT", "winner": "Tim Sheehy", "party": "R",
     "winner_pct": 52.9, "runner_up": "Jon Tester", "runner_up_party": "D",
     "runner_up_pct": 45.0, "winner_votes": 328591, "runner_up_votes": 279573, "margin_pct": 7.9},
    {"year": 2024, "race_type": "senate", "state": "PA", "winner": "Dave McCormick", "party": "R",
     "winner_pct": 48.9, "runner_up": "Bob Casey Jr.", "runner_up_party": "D",
     "runner_up_pct": 48.5, "winner_votes": 3439543, "runner_up_votes": 3421088, "margin_pct": 0.3},
    {"year": 2024, "race_type": "senate", "state": "WI", "winner": "Tammy Baldwin", "party": "D",
     "winner_pct": 49.4, "runner_up": "Eric Hovde", "runner_up_party": "R",
     "runner_up_pct": 48.5, "winner_votes": 1672763, "runner_up_votes": 1643403, "margin_pct": 0.9},
    {"year": 2024, "race_type": "senate", "state": "MI", "winner": "Elissa Slotkin", "party": "D",
     "winner_pct": 48.6, "runner_up": "Mike Rogers", "runner_up_party": "R",
     "runner_up_pct": 48.3, "winner_votes": 2717267, "runner_up_votes": 2697826, "margin_pct": 0.3},
    {"year": 2024, "race_type": "senate", "state": "AZ", "winner": "Ruben Gallego", "party": "D",
     "winner_pct": 50.1, "runner_up": "Kari Lake", "runner_up_party": "R",
     "runner_up_pct": 47.6, "winner_votes": 1700021, "runner_up_votes": 1617240, "margin_pct": 2.5},
    {"year": 2024, "race_type": "senate", "state": "NV", "winner": "Jacky Rosen", "party": "D",
     "winner_pct": 47.9, "runner_up": "Sam Brown", "runner_up_party": "R",
     "runner_up_pct": 46.0, "winner_votes": 710113, "runner_up_votes": 681786, "margin_pct": 1.9},

    # 2022 Midterms — Senate
    {"year": 2022, "race_type": "senate", "state": "GA", "winner": "Raphael Warnock", "party": "D",
     "winner_pct": 51.4, "runner_up": "Herschel Walker", "runner_up_party": "R",
     "runner_up_pct": 48.6, "winner_votes": 1816096, "runner_up_votes": 1719483, "margin_pct": 2.8},
    {"year": 2022, "race_type": "senate", "state": "PA", "winner": "John Fetterman", "party": "D",
     "winner_pct": 51.2, "runner_up": "Mehmet Oz", "runner_up_party": "R",
     "runner_up_pct": 46.3, "winner_votes": 2751012, "runner_up_votes": 2487221, "margin_pct": 4.9},
    {"year": 2022, "race_type": "senate", "state": "AZ", "winner": "Mark Kelly", "party": "D",
     "winner_pct": 51.4, "runner_up": "Blake Masters", "runner_up_party": "R",
     "runner_up_pct": 46.5, "winner_votes": 1322026, "runner_up_votes": 1196308, "margin_pct": 4.9},
    {"year": 2022, "race_type": "senate", "state": "NV", "winner": "Catherine Cortez Masto", "party": "D",
     "winner_pct": 48.8, "runner_up": "Adam Laxalt", "runner_up_party": "R",
     "runner_up_pct": 48.0, "winner_votes": 498316, "runner_up_votes": 490388, "margin_pct": 0.8},
    {"year": 2022, "race_type": "senate", "state": "WI", "winner": "Ron Johnson", "party": "R",
     "winner_pct": 50.4, "runner_up": "Mandela Barnes", "runner_up_party": "D",
     "runner_up_pct": 49.4, "winner_votes": 1336060, "runner_up_votes": 1308372, "margin_pct": 1.0},
    {"year": 2022, "race_type": "senate", "state": "OH", "winner": "JD Vance", "party": "R",
     "winner_pct": 53.0, "runner_up": "Tim Ryan", "runner_up_party": "D",
     "runner_up_pct": 47.0, "winner_votes": 2192567, "runner_up_votes": 1944737, "margin_pct": 6.0},
    {"year": 2022, "race_type": "senate", "state": "NC", "winner": "Ted Budd", "party": "R",
     "winner_pct": 50.5, "runner_up": "Cheri Beasley", "runner_up_party": "D",
     "runner_up_pct": 47.3, "winner_votes": 1898287, "runner_up_votes": 1777905, "margin_pct": 3.2},

    # 2022 Midterms — Governor
    {"year": 2022, "race_type": "governor", "state": "AZ", "winner": "Katie Hobbs", "party": "D",
     "winner_pct": 50.3, "runner_up": "Kari Lake", "runner_up_party": "R",
     "runner_up_pct": 49.7, "winner_votes": 1287890, "runner_up_votes": 1270774, "margin_pct": 0.7},
    {"year": 2022, "race_type": "governor", "state": "GA", "winner": "Brian Kemp", "party": "R",
     "winner_pct": 53.4, "runner_up": "Stacey Abrams", "runner_up_party": "D",
     "runner_up_pct": 45.9, "winner_votes": 2111572, "runner_up_votes": 1813673, "margin_pct": 7.5},
    {"year": 2022, "race_type": "governor", "state": "PA", "winner": "Josh Shapiro", "party": "D",
     "winner_pct": 56.5, "runner_up": "Doug Mastriano", "runner_up_party": "R",
     "runner_up_pct": 41.7, "winner_votes": 3031137, "runner_up_votes": 2239068, "margin_pct": 14.8},
    {"year": 2022, "race_type": "governor", "state": "FL", "winner": "Ron DeSantis", "party": "R",
     "winner_pct": 59.4, "runner_up": "Charlie Crist", "runner_up_party": "D",
     "runner_up_pct": 40.0, "winner_votes": 4614210, "runner_up_votes": 3106316, "margin_pct": 19.4},
    {"year": 2022, "race_type": "governor", "state": "WI", "winner": "Tony Evers", "party": "D",
     "winner_pct": 51.2, "runner_up": "Tim Michels", "runner_up_party": "R",
     "runner_up_pct": 47.8, "winner_votes": 1358151, "runner_up_votes": 1268118, "margin_pct": 3.4},

    # 2020 Presidential
    {"year": 2020, "race_type": "president", "state": "US", "winner": "Joe Biden", "party": "D",
     "winner_pct": 51.3, "runner_up": "Donald Trump", "runner_up_party": "R",
     "runner_up_pct": 46.9, "winner_votes": 81283501, "runner_up_votes": 74223975, "margin_pct": 4.5},

    # 2018 Midterms — key Senate races
    {"year": 2018, "race_type": "senate", "state": "AZ", "winner": "Kyrsten Sinema", "party": "D",
     "winner_pct": 50.0, "runner_up": "Martha McSally", "runner_up_party": "R",
     "runner_up_pct": 47.6, "winner_votes": 1191100, "runner_up_votes": 1135200, "margin_pct": 2.4},
    {"year": 2018, "race_type": "senate", "state": "TX", "winner": "Ted Cruz", "party": "R",
     "winner_pct": 50.9, "runner_up": "Beto O'Rourke", "runner_up_party": "D",
     "runner_up_pct": 48.3, "winner_votes": 4260553, "runner_up_votes": 4045632, "margin_pct": 2.6},
    {"year": 2018, "race_type": "senate", "state": "FL", "winner": "Rick Scott", "party": "R",
     "winner_pct": 50.1, "runner_up": "Bill Nelson", "runner_up_party": "D",
     "runner_up_pct": 49.9, "winner_votes": 4099505, "runner_up_votes": 4089472, "margin_pct": 0.1},
]


def get_results(year: int = None, race_type: str = None, state: str = None) -> list[dict]:
    """Filter historical results."""
    results = HISTORICAL_RESULTS
    if year is not None:
        results = [r for r in results if r["year"] == year]
    if race_type:
        results = [r for r in results if r["race_type"] == race_type]
    if state:
        results = [r for r in results if r["state"].upper() == state.upper()]
    return results


def winning_party(year: int, race_type: str, state: str) -> str | None:
    """Return ``"D"`` / ``"R"`` for the historical winner of (year, race_type, state)."""
    for r in HISTORICAL_RESULTS:
        if r["year"] == year and r["race_type"] == race_type and r["state"].upper() == state.upper():
            return r.get("party")
    return None
