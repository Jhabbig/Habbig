"""World Bank Open Data API.

Free, no API key required. Returns population, GDP, GDP per capita, and
unemployment for any country with an ISO-3 code.

Docs: https://datahelpdesk.worldbank.org/knowledgebase/articles/889392
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from .countries import country_iso3, country_name

logger = logging.getLogger(__name__)

WB_BASE = "https://api.worldbank.org/v2/country"

# World Bank indicator codes
INDICATORS = {
    "population": "SP.POP.TOTL",
    "gdp": "NY.GDP.MKTP.CD",            # GDP current US$
    "gdp_per_capita": "NY.GDP.PCAP.CD",  # GDP per capita current US$
    "unemployment": "SL.UEM.TOTL.ZS",    # Unemployment % of total labor force
    "urban_pct": "SP.URB.TOTL.IN.ZS",    # Urban population % of total
    "life_expectancy": "SP.DYN.LE00.IN",
}


async def _fetch_indicator(
    session: aiohttp.ClientSession, iso3: str, indicator: str
) -> Optional[tuple[float, int]]:
    """Fetch a single indicator's most recent non-null value.

    Returns (value, year) or None.
    """
    url = f"{WB_BASE}/{iso3}/indicator/{indicator}?format=json&per_page=10"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("WorldBank %s/%s failed: %s", iso3, indicator, e)
        return None

    if not isinstance(data, list) or len(data) < 2:
        return None
    rows = data[1] or []
    for row in rows:
        val = row.get("value")
        if val is not None:
            try:
                return float(val), int(row.get("date", 0))
            except (ValueError, TypeError):
                continue
    return None


async def fetch_country_profile(session: aiohttp.ClientSession, country_code: str) -> Optional[dict]:
    """Fetch a country profile from World Bank.

    `country_code` is ISO-2 (e.g. 'HU' for Hungary).
    """
    iso3 = country_iso3(country_code)
    name = country_name(country_code)
    if not iso3:
        logger.warning("WorldBank: no ISO-3 mapping for %s", country_code)
        return None

    pop = await _fetch_indicator(session, iso3, INDICATORS["population"])
    gdp = await _fetch_indicator(session, iso3, INDICATORS["gdp"])
    gdp_pc = await _fetch_indicator(session, iso3, INDICATORS["gdp_per_capita"])
    unemp = await _fetch_indicator(session, iso3, INDICATORS["unemployment"])
    urban = await _fetch_indicator(session, iso3, INDICATORS["urban_pct"])
    life = await _fetch_indicator(session, iso3, INDICATORS["life_expectancy"])

    profile: dict = {
        "name": name,
        "country_code": country_code.upper(),
        "iso3": iso3,
        "population": {
            "total": int(pop[0]) if pop else 0,
            "year": pop[1] if pop else 0,
        },
        "demographics": {
            "urban_pct": round(urban[0], 1) if urban else None,
            "life_expectancy": round(life[0], 1) if life else None,
        },
        "economy": {
            "gdp_billions": round(gdp[0] / 1e9, 0) if gdp else None,
            "gdp_per_capita": round(gdp_pc[0], 0) if gdp_pc else None,
            "unemployment_rate": round(unemp[0], 1) if unemp else None,
            "year": gdp[1] if gdp else 0,
        },
        "_source": {
            "name": "World Bank Open Data",
            "url": f"https://data.worldbank.org/country/{iso3}",
        },
    }
    return profile
