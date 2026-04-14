"""Unified enrichment dispatcher.

Takes a base profile (from district_profiles.py static data, or empty) and
merges in fresh data from the live data sources. Routes by jurisdiction:

- US state          → Census ACS + BEA + BLS
- US House district → Census ACS (district level)
- Country (world)   → World Bank + Wikipedia

The merge is non-destructive: live data fills in fields the static profile
doesn't have, and overwrites stale numeric fields when fresh data is more
recent. Hand-curated narrative fields (`summary`, `key_facts`) are preserved.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Optional

import aiohttp

from .bea import fetch_state_gdp
from .bls import fetch_state_unemployment
from .census import fetch_house_district_demographics, fetch_state_demographics
from .countries import country_adjective, country_name
from .fips import state_to_name
from .world_bank import fetch_country_profile
from .wikipedia import (
    fetch_country_political_summary,
    fetch_governor_past_winners,
    fetch_house_district_elections,
    fetch_house_district_past_winners,
    fetch_house_district_summary,
    fetch_recent_elections,
    fetch_senate_past_winners,
    fetch_state_elections,
    fetch_state_political_summary,
)

logger = logging.getLogger(__name__)


def _merge(base: dict, fresh: dict) -> dict:
    """Deep-merge `fresh` into `base`. Lists are replaced; dicts are merged.

    Numeric values from `fresh` overwrite `base` only when not None.
    String values: keep `base` if non-empty (preserves curated narratives).
    """
    out = deepcopy(base) if base else {}
    for k, v in fresh.items():
        if v is None:
            continue
        if isinstance(v, dict):
            out[k] = _merge(out.get(k, {}) if isinstance(out.get(k), dict) else {}, v)
        elif isinstance(v, list):
            # Lists from fresh data win only if base list is empty
            if not out.get(k):
                out[k] = v
        elif isinstance(v, str):
            # Preserve curated narratives unless base is empty
            if not out.get(k):
                out[k] = v
        else:
            # Numeric: prefer fresh over stale
            out[k] = v
    return out


# ============================================================================
# US state enrichment
# ============================================================================


async def enrich_state_profile(
    session: aiohttp.ClientSession, state: str, base: Optional[dict] = None
) -> dict:
    """Enrich a US state profile with live Census/BEA/BLS + Wikipedia history."""
    base = base or {}
    enriched: dict = deepcopy(base)
    state_postal = state.upper()
    state_name = state_to_name(state_postal)

    census = await fetch_state_demographics(session, state)
    if census:
        enriched = _merge(enriched, census)

    gdp = await fetch_state_gdp(session, state)
    if gdp:
        enriched.setdefault("economy", {})["gdp_billions"] = gdp.get("gdp_billions")
        enriched["economy"]["gdp_year"] = gdp.get("year")

    bls = await fetch_state_unemployment(session, state)
    if bls:
        enriched.setdefault("economy", {})["unemployment_rate"] = bls.get("unemployment_rate")
        enriched["economy"]["unemployment_period"] = bls.get("period")

    # Wikipedia: politics-of summary + structured list of past elections
    pol = await fetch_state_political_summary(session, state_postal, state_name)
    wiki_used = False
    if pol:
        enriched.setdefault("political_history", {})
        if not enriched["political_history"].get("summary"):
            enriched["political_history"]["summary"] = pol.get("extract", "")
        enriched["political_history"]["wikipedia_url"] = pol.get("url")
        if pol.get("thumbnail") and not enriched.get("thumbnail"):
            enriched["thumbnail"] = pol["thumbnail"]
        wiki_used = True

    elections = await fetch_state_elections(
        session, state_postal, state_name, max_results=8
    )
    if elections:
        enriched["recent_elections"] = elections
        wiki_used = True

    # Per-race past winners parsed from individual election articles
    senate_winners = await fetch_senate_past_winners(
        session, state_postal, state_name, max_results=5
    )
    if senate_winners:
        enriched["senate_past_winners"] = senate_winners
        wiki_used = True

    governor_winners = await fetch_governor_past_winners(
        session, state_postal, state_name, max_results=4
    )
    if governor_winners:
        enriched["governor_past_winners"] = governor_winners
        wiki_used = True

    enriched["_enriched_at"] = _now()
    enriched.setdefault("_data_sources", []).extend([
        s for s in [
            "Census ACS",
            "BEA" if gdp else None,
            "BLS" if bls else None,
            "Wikipedia" if wiki_used else None,
        ] if s
    ])
    return enriched


# ============================================================================
# US House district enrichment
# ============================================================================


async def enrich_house_district_profile(
    session: aiohttp.ClientSession,
    state: str,
    district: str,
    base: Optional[dict] = None,
) -> dict:
    """Enrich a House district profile with district-level Census data + Wikipedia history.

    Wikipedia gives us:
    - District article summary (current rep, redistricting context, geography)
    - State-cycle House election articles (most recent N cycles)
    """
    enriched: dict = deepcopy(base or {})
    state_postal = state.upper()
    state_name = state_to_name(state_postal)

    census = await fetch_house_district_demographics(session, state, district)
    if census:
        enriched = _merge(enriched, census)

    # District article: current rep + redistricting overview
    district_summary = await fetch_house_district_summary(session, state_name, district)
    wiki_used = False
    if district_summary:
        enriched.setdefault("political_history", {})
        if not enriched["political_history"].get("summary"):
            enriched["political_history"]["summary"] = district_summary.get("extract", "")
        enriched["political_history"]["wikipedia_url"] = district_summary.get("url")
        enriched["political_history"]["title"] = district_summary.get("title")
        if district_summary.get("description"):
            enriched["political_history"]["description"] = district_summary["description"]
        if district_summary.get("thumbnail") and not enriched.get("thumbnail"):
            enriched["thumbnail"] = district_summary["thumbnail"]
        wiki_used = True

    # State-cycle house elections (most recent 6) — gives the year-by-year context
    cycle_elections = await fetch_house_district_elections(
        session, state_postal, state_name, district, max_results=6
    )
    if cycle_elections:
        enriched["recent_elections"] = cycle_elections
        wiki_used = True

    # Per-district winners parsed from cycle articles' Election box templates.
    # Three cycles back from 2024 (the most recent completed House general).
    past_winners = await fetch_house_district_past_winners(
        session, state_postal, state_name, district, cycles=3, latest_year=2024
    )
    if past_winners:
        enriched["past_winners"] = past_winners
        wiki_used = True

    enriched["state"] = state_postal
    enriched["district"] = district
    enriched["_enriched_at"] = _now()
    enriched.setdefault("_data_sources", []).append("Census ACS (district)")
    if wiki_used:
        enriched["_data_sources"].append("Wikipedia")
    return enriched


# ============================================================================
# International / country enrichment
# ============================================================================


async def enrich_country_profile(
    session: aiohttp.ClientSession,
    country_code: str,
    base: Optional[dict] = None,
) -> dict:
    """Build a country profile from World Bank + Wikipedia."""
    enriched: dict = deepcopy(base or {})
    wb = await fetch_country_profile(session, country_code)
    if wb:
        enriched = _merge(enriched, wb)

    name = country_name(country_code) or country_code
    pol = await fetch_country_political_summary(session, name)
    if pol:
        enriched.setdefault("political_history", {})["summary"] = pol.get("extract", "")
        enriched["political_history"]["wikipedia_url"] = pol.get("url")
        if pol.get("thumbnail"):
            enriched["thumbnail"] = pol["thumbnail"]

    adjective = country_adjective(country_code)
    elections = await fetch_recent_elections(session, name, max_results=5, adjective=adjective)
    if elections:
        enriched["recent_elections"] = elections

    enriched["country_code"] = country_code.upper()
    enriched["name"] = name
    enriched["jurisdiction_type"] = "country"
    enriched["_enriched_at"] = _now()
    enriched.setdefault("_data_sources", []).extend(["World Bank", "Wikipedia"])
    return enriched


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
