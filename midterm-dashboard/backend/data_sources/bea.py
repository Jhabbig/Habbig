"""Bureau of Economic Analysis (BEA) GDP data.

Requires a free API key from https://apps.bea.gov/API/signup/.
Set BEA_API_KEY in env to enable; without a key this module returns None
and the dashboard falls back to static GDP estimates.

Docs: https://apps.bea.gov/API/docs/index.htm
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

from .fips import state_to_fips

logger = logging.getLogger(__name__)

BEA_BASE = "https://apps.bea.gov/api/data"


async def fetch_state_gdp(session: aiohttp.ClientSession, state: str) -> Optional[dict]:
    """Fetch state GDP from BEA Regional Economic Accounts.

    Returns {gdp_billions: float, year: int} or None.
    """
    key = os.getenv("BEA_API_KEY", "").strip()
    if not key:
        return None

    fips = state_to_fips(state)
    if not fips:
        return None

    # SAGDP2N = State annual GDP in current dollars (millions)
    # LineCode 1 = All industry total
    params = {
        "UserID": key,
        "method": "GetData",
        "datasetname": "Regional",
        "TableName": "SAGDP2N",
        "LineCode": "1",
        "GeoFIPS": f"{fips}000",
        "Year": "LAST1",
        "ResultFormat": "JSON",
    }
    try:
        async with session.get(BEA_BASE, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("BEA GDP %s fetch failed: %s", state, e)
        return None

    try:
        results = data.get("BEAAPI", {}).get("Results", {})
        if isinstance(results, list):
            results = results[0] if results else {}
        rows = results.get("Data", [])
        if not rows:
            return None
        row = rows[0]
        # DataValue is in millions of dollars
        value_str = str(row.get("DataValue", "0")).replace(",", "")
        gdp_millions = float(value_str)
        gdp_billions = round(gdp_millions / 1000, 0)
        year = int(row.get("TimePeriod", 0))
        return {
            "gdp_billions": gdp_billions,
            "year": year,
            "_source": "BEA Regional Accounts SAGDP2N",
        }
    except (KeyError, ValueError, IndexError, TypeError) as e:
        logger.warning("BEA GDP %s parse failed: %s", state, e)
        return None
