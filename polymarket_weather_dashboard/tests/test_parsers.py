"""Fixture-driven tests for the title parsers.

The parsers (parse_city, parse_temperature, parse_date) silently decide
which markets get a model probability and which fall through to None.
Every regression here used to be invisible until trade results dried up;
these fixtures make the breakage loud.
"""

from datetime import datetime, timezone

import pytest

from weather_pure import (
    make_city_parser,
    parse_date,
    parse_temperature,
    parse_threshold_for_resolution,
    resolve_market,
)


# Mirror server.py: STATION_MAP has "new york" and "nyc" as separate
# canonical keys mapping to the same coords; CITY_ALIASES handles
# colloquial spellings. We mirror that exactly here so the tests verify
# *actual* parser behavior.
_CITY_KEYS = {
    "new york", "nyc", "chicago", "dallas", "miami", "los angeles", "la",
    "london", "paris", "tokyo", "san francisco", "austin", "denver",
    "seattle", "atlanta",
}
_ALIASES = {
    "new york city": "new york", "manhattan": "new york",
    "sf": "san francisco", "dfw": "dallas",
}

parse_city = make_city_parser(_CITY_KEYS, _ALIASES)


# ─── parse_city ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    # "nyc" is its own canonical key (matches server.py STATION_MAP)
    ("Highest temperature in NYC on April 12?", "nyc"),
    ("Will Manhattan hit 80°F?", "new york"),
    ("New York City high above 70?", "new york"),
    ("San Francisco daily max May 1", "san francisco"),
    ("SF daily high — June 1, 2026", "san francisco"),
    ("LA daily high temperature?", "la"),
    ("Highest temp in DFW", "dallas"),
    ("Highest temp in Toronto", None),  # Toronto not in this fixture set
    ("vandals win the cup", None),     # word-boundary: must not match "dallas"
    ("",                          None),
    ("Climate change: warming",   None),
])
def test_parse_city_basic(title, expected):
    assert parse_city(title) == expected


def test_parse_city_known_limitation_dot_variants():
    """`L.A.` (with periods) doesn't match the \\b-anchored regex that the
    parser uses, even when the alias is registered. Documenting this so
    nobody is surprised — the workaround is to add 'la' as a substring
    match or to normalize titles before parsing."""
    p = make_city_parser({"la"}, {"l.a.": "la"})
    # Confirms current behavior — change requires a parser fix, not a fixture fix.
    assert p("L.A. high?") is None


# ─── parse_temperature ────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("NYC high above 70°F",
        {"threshold": 70.0, "is_over": True, "unit": "F"}),
    ("Will the high be 75 or higher?",
        {"threshold": 75.0, "is_over": True, "unit": "F"}),
    ("High temperature ≥ 90 in Dallas",
        {"threshold": 90.0, "is_over": True, "unit": "F"}),
    ("Will the high be below 50°F?",
        {"threshold": 50.0, "is_over": False, "unit": "F"}),
    ("High under 60°F",
        {"threshold": 60.0, "is_over": False, "unit": "F"}),
    ("High temperature 65-66°F",
        {"temp_lower": 65.0, "temp_upper": 66.0, "unit": "F"}),
    ("Between 70 and 75°F",
        {"temp_lower": 70.0, "temp_upper": 75.0, "unit": "F"}),
    ("Global temp anomaly above 1.5°C",
        {"threshold": 1.5, "is_over": True, "unit": "C"}),
    ("Annual mean below 1.4°C",
        {"threshold": 1.4, "is_over": False, "unit": "C"}),
    ("Between 1.5°C and 1.7°C",
        {"temp_lower": 1.5, "temp_upper": 1.7, "unit": "C"}),
    # Non-temperature markets that contain numbers should NOT score
    ("Major hurricane landfall in Florida — 5+ inches of rain",
        None),
    ("Earthquake of magnitude 7+ in California",
        None),
    ("Total snowfall above 12 inches in NYC",
        None),
    ("",
        None),
])
def test_parse_temperature(title, expected):
    result = parse_temperature(title)
    if expected is None:
        # all fields should be None / default
        assert result["threshold"] is None
        assert result["temp_lower"] is None
        assert result["temp_upper"] is None
        return
    for k, v in expected.items():
        assert result[k] == v, f"{title!r} field {k}: got {result[k]} != {v}"


def test_parse_temperature_range_overrides_single():
    # "between 70 and 75" should produce a range, not a single threshold
    out = parse_temperature("Will the high be between 70 and 75°F?")
    assert out["temp_lower"] == 70.0
    assert out["temp_upper"] == 75.0
    assert out["threshold"] is None


# ─── parse_date ───────────────────────────────────────────────────────────────

# Inject a fixed "today" so tests are deterministic regardless of when run.
TODAY = datetime(2026, 5, 6, tzinfo=timezone.utc)


@pytest.mark.parametrize("title,expected", [
    ("High temp in NYC on May 12 above 80?",   "2026-05-12"),
    ("Daily max May 6 in Dallas",              "2026-05-06"),
    ("April 1, 2027 — Chicago",                "2027-04-01"),
    ("June 2026 monthly high",                 "2026-06-15"),
    ("ISO date: 2026-07-04",                   "2026-07-04"),
    # Bumps to next year when the implied month-day is >30d in the past
    ("January 5 high temperature",             "2027-01-05"),
    # No date at all
    ("Will it rain tomorrow?",                 None),
])
def test_parse_date(title, expected):
    assert parse_date(title, today=TODAY) == expected


def test_parse_date_avoids_false_match_on_month_abbrevs():
    # "may" shouldn't match "mayor", "maybe", "market"
    assert parse_date("Mayor's office statement", today=TODAY) is None
    assert parse_date("Maybe the high above 70?", today=TODAY) is None
    assert parse_date("Market closes Friday", today=TODAY) is None


# ─── parse_threshold_for_resolution + resolve_market ──────────────────────────

@pytest.mark.parametrize("question,expected", [
    ("Will the high be above 70?",     ("above", 70, None)),
    ("High at least 85",               ("above", 85, None)),
    ("65 or higher",                    ("above", 65, None)),
    ("High below 50",                   ("below", 50, None)),
    ("50 or lower",                     ("below", 50, None)),
    ("Between 64 and 65",               ("between", 64, 65)),
    ("Between 64-65",                   ("between", 64, 65)),
    ("Will it rain?",                   (None, None, None)),
])
def test_parse_threshold_for_resolution(question, expected):
    assert parse_threshold_for_resolution(question) == expected


@pytest.mark.parametrize("observed,threshold,expected", [
    (72.0, ("above", 70, None),    True),
    (69.9, ("above", 70, None),    False),
    (70.0, ("above", 70, None),    True),    # >=, equality wins
    (50.0, ("below", 50, None),    True),    # <=, equality wins
    (51.0, ("below", 50, None),    False),
    (64.5, ("between", 64, 65),    True),
    (65.0, ("between", 64, 65),    True),
    (63.9, ("between", 64, 65),    False),
    (None, ("above", 70, None),    None),
    (72.0, (None,    None, None),  None),
])
def test_resolve_market(observed, threshold, expected):
    assert resolve_market(observed, threshold) == expected
