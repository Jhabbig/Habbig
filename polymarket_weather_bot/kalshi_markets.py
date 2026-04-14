"""Kalshi weather temperature market fetcher (KXHIGH series).

Adapted from suislanchez/polymarket-kalshi-weather-bot (MIT).
Returns the same WeatherMarket dataclass used by the Polymarket path so the
entire edge → sizing → execution pipeline works unchanged.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import aiohttp

from gamma_client import WeatherMarket
from city_stations import lookup_station

logger = logging.getLogger(__name__)

# Kalshi series tickers for high-temperature markets by city
CITY_SERIES: Dict[str, str] = {
    "new york":    "KXHIGHNY",
    "chicago":     "KXHIGHCHI",
    "miami":       "KXHIGHMIA",
    "los angeles": "KXHIGHLAX",
    "denver":      "KXHIGHDEN",
}

MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_kalshi_ticker(ticker: str) -> Optional[dict]:
    """Parse a Kalshi bracket ticker into market parameters.

    Format: KXHIGHNY-26MAR01-B45.5
      - 26MAR01 = 2026-03-01
      - B45.5 = bracket boundary at 45.5 F (above)
      - T45.5 = top boundary (at or below)
    """
    match = re.match(
        r'^[A-Z]+-(\d{2})([A-Z]{3})(\d{2})-([BT])([\d.]+)$',
        ticker,
    )
    if not match:
        return None

    yy = int(match.group(1))
    mon_str = match.group(2)
    dd = int(match.group(3))
    boundary_type = match.group(4)
    threshold = float(match.group(5))

    month = MONTH_ABBR.get(mon_str)
    if not month:
        return None

    year = 2000 + yy
    try:
        target_date = date(year, month, dd)
    except ValueError:
        return None

    # B = bottom boundary -> "above" threshold; T = top boundary -> "below"
    is_over = boundary_type == "B"

    return {
        "target_date": target_date,
        "threshold": threshold,
        "is_over": is_over,
    }


async def fetch_kalshi_weather_markets(
    session: aiohttp.ClientSession,
    kalshi_client,
    city_keys: Optional[List[str]] = None,
) -> List[WeatherMarket]:
    """Fetch open weather temperature markets from Kalshi.

    Queries the KXHIGH{city} series for each configured city,
    handles cursor-based pagination, and returns WeatherMarket objects
    compatible with the existing Polymarket pipeline.

    Args:
        session: Shared aiohttp session.
        kalshi_client: Authenticated KalshiClient instance.
        city_keys: Optional list of city names to scan (e.g. ["new york", "chicago"]).
                   Defaults to all configured cities.
    """
    markets: List[WeatherMarket] = []
    today = date.today()

    cities = city_keys or list(CITY_SERIES.keys())

    for city_name in cities:
        series = CITY_SERIES.get(city_name)
        if not series:
            continue

        station = lookup_station(city_name)
        if not station:
            logger.warning("No station mapping for city '%s', skipping Kalshi", city_name)
            continue

        lat, lon, icao, station_display = station
        cursor = None

        try:
            while True:
                params = {
                    "series_ticker": series,
                    "status": "open",
                    "limit": "200",
                }
                if cursor:
                    params["cursor"] = cursor

                data = await kalshi_client.get_markets(session, params)
                raw_markets = data.get("markets", [])

                for m in raw_markets:
                    ticker = m.get("ticker", "")
                    parsed = _parse_kalshi_ticker(ticker)
                    if not parsed:
                        continue

                    if parsed["target_date"] < today:
                        continue

                    # Kalshi prices are in cents (0-100), convert to 0-1
                    yes_price = (m.get("yes_ask") or 0) / 100.0
                    if yes_price <= 0:
                        yes_price = (m.get("last_price") or 50) / 100.0

                    # Skip fully resolved or illiquid
                    if yes_price > 0.98 or yes_price < 0.02:
                        continue

                    volume = float(m.get("volume", 0) or 0)
                    target_dt = datetime(
                        parsed["target_date"].year,
                        parsed["target_date"].month,
                        parsed["target_date"].day,
                        tzinfo=timezone.utc,
                    )

                    markets.append(WeatherMarket(
                        condition_id=ticker,
                        question=m.get("title", ticker),
                        market_slug=ticker,
                        city=city_name,
                        station_icao=icao,
                        lat=lat,
                        lon=lon,
                        target_date=target_dt,
                        temp_lower=None,
                        temp_upper=None,
                        threshold=parsed["threshold"],
                        is_over=parsed["is_over"],
                        market_price=yes_price,
                        volume=volume,
                        liquidity=0.0,
                        outcome="Yes",
                        token_id="",
                        no_token_id="",
                        end_date=target_dt,
                        platform="kalshi",
                    ))

                # Handle cursor-based pagination
                cursor = data.get("cursor")
                if not cursor or not raw_markets:
                    break

        except Exception as e:
            logger.warning("Failed to fetch Kalshi markets for %s (%s): %s",
                           city_name, series, e)

    logger.info("Found %d Kalshi weather markets", len(markets))
    return markets
