"""Bureau of Labor Statistics (BLS) state unemployment data.

Free public API. Without a registered key it's limited to 25 requests/day per
IP, but state unemployment is a single call so this is fine for our use.

Docs: https://www.bls.gov/developers/
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

from .fips import state_to_fips

logger = logging.getLogger(__name__)

BLS_BASE = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


async def fetch_state_unemployment(session: aiohttp.ClientSession, state: str) -> Optional[dict]:
    """Fetch most-recent state unemployment rate from BLS LAUS.

    Series ID format: LASST{FIPS}0000000000003 = state unemployment rate
    """
    fips = state_to_fips(state)
    if not fips:
        return None

    series_id = f"LASST{fips}0000000000003"
    payload = {"seriesid": [series_id]}
    key = os.getenv("BLS_API_KEY", "").strip()
    if key:
        payload["registrationkey"] = key

    try:
        async with session.post(
            BLS_BASE,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("BLS state %s failed: %s", state, e)
        return None

    try:
        results = data.get("Results", {}).get("series", [])
        if not results:
            return None
        items = results[0].get("data", [])
        if not items:
            return None
        latest = items[0]
        return {
            "unemployment_rate": float(latest.get("value", 0)),
            "period": f"{latest.get('year')}-{latest.get('period', '')}",
            "_source": "BLS LAUS",
        }
    except (KeyError, ValueError, IndexError, TypeError) as e:
        logger.warning("BLS state %s parse failed: %s", state, e)
        return None
