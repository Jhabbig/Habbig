"""External data source adapters for the midterm dashboard.

Each module wraps a government / public API and returns normalized dicts
matching the profile schema (demographics, economy, etc.). Modules degrade
gracefully when API keys are missing so the dashboard still runs without
configuration.
"""

from .census import fetch_state_demographics, fetch_house_district_demographics
from .bea import fetch_state_gdp
from .bls import fetch_state_unemployment
from .world_bank import fetch_country_profile
from .wikipedia import (
    fetch_country_political_summary,
    fetch_recent_elections,
    fetch_person_bio,
)
from .enrich import (
    enrich_state_profile,
    enrich_house_district_profile,
    enrich_country_profile,
)

__all__ = [
    "fetch_state_demographics",
    "fetch_house_district_demographics",
    "fetch_state_gdp",
    "fetch_state_unemployment",
    "fetch_country_profile",
    "fetch_country_political_summary",
    "fetch_recent_elections",
    "fetch_person_bio",
    "enrich_state_profile",
    "enrich_house_district_profile",
    "enrich_country_profile",
]
