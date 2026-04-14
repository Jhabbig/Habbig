"""Census Bureau ACS (American Community Survey) 5-year estimates.

Uses the free public API at https://api.census.gov/data/.  Without a key
it's rate-limited to 500 requests/day per IP which is plenty for our use.
With CENSUS_API_KEY set in env it lifts the limit.

Docs: https://www.census.gov/data/developers/data-sets/acs-5year.html
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

from .fips import state_to_fips, state_to_name

logger = logging.getLogger(__name__)

ACS_YEAR = 2023  # Most recent 5-year estimates (2019-2023)
BASE_URL = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"

# Census variable codes:
#   B01003_001E = Total population
#   B01002_001E = Median age
#   B02001_002E = White alone
#   B02001_003E = Black alone
#   B02001_005E = Asian alone
#   B03003_003E = Hispanic or Latino
#   B19013_001E = Median household income
#   B15003_022E = Bachelor's degree (25+ pop with bachelor's)
#   B15003_001E = Total 25+ population (denominator for bachelor's %)
#   B23025_005E = Unemployed (civilian labor force)
#   B23025_002E = In labor force (denominator for unemployment %)
#   B25010_001E = Average household size

_VARS = [
    "B01003_001E",  # total pop
    "B01002_001E",  # median age
    "B02001_002E",  # white
    "B02001_003E",  # black
    "B02001_005E",  # asian
    "B03003_003E",  # hispanic
    "B19013_001E",  # median hh income
    "B15003_022E",  # bachelor's
    "B15003_001E",  # 25+ pop total
    "B23025_005E",  # unemployed
    "B23025_002E",  # labor force
]


def _api_key_param() -> str:
    k = os.getenv("CENSUS_API_KEY", "").strip()
    return f"&key={k}" if k else ""


def _pct(num: Optional[float], denom: Optional[float], digits: int = 1) -> Optional[float]:
    if num is None or denom is None or denom == 0:
        return None
    return round((num / denom) * 100, digits)


def _parse_row(header: list[str], row: list[str]) -> dict:
    """Turn the paired [header, row] Census response into a {var: float} dict."""
    out = {}
    for h, v in zip(header, row):
        if h in _VARS:
            try:
                out[h] = float(v) if v not in (None, "", "null") else None
            except (ValueError, TypeError):
                out[h] = None
    return out


def _build_profile(state_abbr: str, parsed: dict) -> dict:
    """Shape parsed Census vars into the profile schema used by the dashboard."""
    pop_total = parsed.get("B01003_001E")
    white = parsed.get("B02001_002E")
    black = parsed.get("B02001_003E")
    asian = parsed.get("B02001_005E")
    hisp = parsed.get("B03003_003E")
    bach = parsed.get("B15003_022E")
    bach_denom = parsed.get("B15003_001E")
    unemployed = parsed.get("B23025_005E")
    labor_force = parsed.get("B23025_002E")

    return {
        "population": {
            "total": int(pop_total) if pop_total else 0,
            "year": ACS_YEAR,
        },
        "demographics": {
            "white": _pct(white, pop_total),
            "black": _pct(black, pop_total),
            "hispanic": _pct(hisp, pop_total),
            "asian": _pct(asian, pop_total),
            "median_age": parsed.get("B01002_001E"),
        },
        "economy": {
            "median_household_income": int(parsed["B19013_001E"]) if parsed.get("B19013_001E") else None,
            "unemployment_rate": _pct(unemployed, labor_force),
        },
        "education": {
            "bachelors_or_higher_pct": _pct(bach, bach_denom),
        },
        "_source": {
            "name": "US Census Bureau ACS 5-year",
            "year": ACS_YEAR,
            "url": f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5",
        },
    }


async def fetch_state_demographics(session: aiohttp.ClientSession, state: str) -> Optional[dict]:
    """Fetch demographics for an entire US state from ACS 5-year.

    Returns a profile-shaped dict or None on failure.
    """
    fips = state_to_fips(state)
    if not fips:
        logger.warning("Census: no FIPS code for state %s", state)
        return None

    url = (
        f"{BASE_URL}?get={','.join(_VARS)}"
        f"&for=state:{fips}{_api_key_param()}"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning("Census state %s: HTTP %s", state, resp.status)
                return None
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("Census state %s fetch failed: %s", state, e)
        return None

    if not data or len(data) < 2:
        return None

    header, row = data[0], data[1]
    parsed = _parse_row(header, row)
    profile = _build_profile(state.upper(), parsed)
    profile["name"] = state_to_name(state)
    return profile


async def fetch_house_district_demographics(
    session: aiohttp.ClientSession, state: str, district: str
) -> Optional[dict]:
    """Fetch demographics for a single US House congressional district.

    `district` is a string: "01", "02", ..., "AL" for at-large.  The Census
    at-large code is "00" for ACS 5-year.
    """
    fips = state_to_fips(state)
    if not fips:
        return None

    # At-large: Census uses "00"
    d = district.upper()
    if d in ("AL", "0", "00", ""):
        census_district = "00"
    else:
        # Pad to 2 digits
        try:
            census_district = f"{int(d):02d}"
        except ValueError:
            return None

    url = (
        f"{BASE_URL}?get={','.join(_VARS)}"
        f"&for=congressional%20district:{census_district}"
        f"&in=state:{fips}{_api_key_param()}"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning(
                    "Census district %s-%s: HTTP %s", state, district, resp.status
                )
                return None
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("Census district %s-%s fetch failed: %s", state, district, e)
        return None

    if not data or len(data) < 2:
        return None

    header, row = data[0], data[1]
    parsed = _parse_row(header, row)
    profile = _build_profile(state.upper(), parsed)
    profile["name"] = f"{state_to_name(state)} District {district}"
    profile["district"] = district
    return profile
