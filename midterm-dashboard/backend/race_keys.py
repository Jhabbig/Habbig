"""Race key parsing and jurisdiction lookups.

A `race_key` uniquely identifies a race on the dashboard. Format:
  - senate_GA            (statewide US race)
  - governor_PA          (statewide US race)
  - house_TX-28          (US House district)
  - presidential_US      (national US race)
  - world_HU             (international country-level race)
  - world_HU-pres-2026   (specific named international race)

For the unified profile dispatcher we extract:
  jurisdiction_type: 'us_state' | 'us_district' | 'country'
  jurisdiction_code: 'GA' | 'TX-28' | 'HU'
"""

from __future__ import annotations

import re

# Word-form district number → numeric (matches both ordinals and cardinals)
_ORDINAL_TO_NUM: dict[str, int] = {
    "first": 1, "1st": 1, "1": 1,
    "second": 2, "2nd": 2, "2": 2,
    "third": 3, "3rd": 3, "3": 3,
    "fourth": 4, "4th": 4, "4": 4,
    "fifth": 5, "5th": 5, "5": 5,
    "sixth": 6, "6th": 6, "6": 6,
    "seventh": 7, "7th": 7, "7": 7,
    "eighth": 8, "8th": 8, "8": 8,
    "ninth": 9, "9th": 9, "9": 9,
    "tenth": 10, "10th": 10, "10": 10,
    "eleventh": 11, "11th": 11, "11": 11,
    "twelfth": 12, "12th": 12, "12": 12,
}

# Regexes for different title patterns
_DISTRICT_PATTERNS = [
    # "Maine's 2nd District", "Texas's 28th Congressional District"
    re.compile(r"(?:[A-Z][a-z]+(?:\s[A-Z][a-z]+)*)['\u2019]s\s+(\d+)(?:st|nd|rd|th)?\s+(?:Congressional\s+)?District", re.IGNORECASE),
    # "TX-28", "TX 28"
    re.compile(r"\b([A-Z]{2})[-\s](\d{1,2})\b"),
    # "District 28 of Texas"
    re.compile(r"District\s+(\d+)\s+of", re.IGNORECASE),
]


def parse_district_from_title(title: str) -> str | None:
    """Extract a district number (e.g. '02', '28') from a House market title.

    Returns None if no district found. At-large districts return '00'.
    """
    if not title:
        return None

    # Try numeric patterns first
    for pat in _DISTRICT_PATTERNS:
        m = pat.search(title)
        if m:
            # Last numeric group is the district
            num_str = m.group(m.lastindex)
            try:
                num = int(num_str)
                if 1 <= num <= 60:  # sanity bound
                    return f"{num:02d}"
            except (ValueError, TypeError):
                continue

    # At-large hint
    if re.search(r"\bat[-\s]?large\b", title, re.IGNORECASE):
        return "00"

    return None


def race_key_to_jurisdiction(race_key: str, race_type: str = "", state: str = "", title: str = "") -> tuple[str, str, str]:
    """Convert a race + metadata to (jurisdiction_type, jurisdiction_code, display_name).

    Examples:
        ('senate_GA', 'senate', 'GA') → ('us_state', 'GA', 'Georgia')
        ('house_TX', 'house', 'TX', "Texas's 28th District") → ('us_district', 'TX-28', 'Texas District 28')
        ('world_HU', 'world', 'HU') → ('country', 'HU', 'Hungary')
    """
    rt = race_type.lower() if race_type else ""
    st = (state or "").upper()

    # International
    if rt == "world":
        from data_sources.countries import country_name
        return ("country", st, country_name(st) or st)

    # House districts
    if rt == "house":
        district = parse_district_from_title(title)
        if district:
            from data_sources.fips import state_to_name
            code = f"{st}-{district}"
            name = f"{state_to_name(st)} District {int(district)}" if district != "00" else f"{state_to_name(st)} (At-large)"
            return ("us_district", code, name)
        # House race without parseable district → fall back to state
        from data_sources.fips import state_to_name
        return ("us_state", st, state_to_name(st))

    # Statewide US races (senate, governor, presidential, control)
    if st and st != "US":
        from data_sources.fips import state_to_name
        return ("us_state", st, state_to_name(st))

    return ("us_state", "US", "United States")
