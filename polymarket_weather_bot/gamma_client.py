"""Polymarket Gamma API client — fetch active weather/temperature markets."""

from __future__ import annotations

import re
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

import aiohttp

from city_stations import lookup_station, get_all_city_keywords

logger = logging.getLogger(__name__)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


@dataclass
class WeatherMarket:
    """Parsed weather market from Polymarket."""
    condition_id: str
    question: str
    market_slug: str
    city: str
    station_icao: str
    lat: float
    lon: float
    target_date: Optional[datetime]
    temp_lower: Optional[float]   # Lower bound of temperature bucket (°F)
    temp_upper: Optional[float]   # Upper bound of temperature bucket (°F)
    threshold: Optional[float]    # Single threshold for over/under markets
    is_over: Optional[bool]       # True = "over X°", False = "under X°", None = bucket
    market_price: float           # Current YES price (0-1)
    volume: float
    liquidity: float
    outcome: str                  # "Yes" or "No"
    token_id: str
    end_date: Optional[datetime]


async def fetch_weather_markets(session: aiohttp.ClientSession) -> list:
    """Fetch all active weather-related markets from Gamma API.

    Strategy: paginate through events, filter by weather/climate tags and
    temperature-related keywords in titles, then extract their sub-markets.
    """
    all_markets = []
    seen_ids = set()

    WEATHER_TAGS = {"weather", "climate", "climate & weather", "climate change", "global temp"}
    TITLE_KEYWORDS = [
        "temperature", "highest temp", "hottest", "coldest", "heat wave",
        "degrees fahrenheit", "°f", "precipitation", "rainfall",
    ]

    # Paginate through all open events to find weather-related ones
    offset = 0
    max_pages = 30  # Check up to 3000 events

    for _ in range(max_pages):
        params = {"closed": "false", "limit": "100", "offset": str(offset)}
        try:
            async with session.get(f"{GAMMA_BASE_URL}/events", params=params) as resp:
                if resp.status != 200:
                    logger.warning("Gamma events API returned %d", resp.status)
                    break

                events = await resp.json()
                if not events:
                    break

                for event in events:
                    title = (event.get("title", "") or "").lower()
                    tags = event.get("tags", [])
                    tag_labels = {t.get("label", "").lower() for t in tags if isinstance(t, dict)}
                    tag_slugs = {t.get("slug", "").lower() for t in tags if isinstance(t, dict)}
                    all_tags = tag_labels | tag_slugs

                    is_weather = bool(all_tags & WEATHER_TAGS) or \
                                 any(k in title for k in TITLE_KEYWORDS)

                    if is_weather:
                        # Extract individual markets from the event
                        event_markets = event.get("markets", [])
                        for m in event_markets:
                            mid = m.get("conditionId") or m.get("id", "")
                            if mid and mid not in seen_ids:
                                seen_ids.add(mid)
                                all_markets.append(m)

                offset += 100

        except Exception as e:
            logger.error("Error fetching events page at offset %d: %s", offset, e)
            break

    # Also try direct market searches as backup
    for keyword in ["temperature", "highest temperature"]:
        params = {"closed": "false", "active": "true", "limit": "100"}
        try:
            async with session.get(f"{GAMMA_BASE_URL}/markets", params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        for m in data:
                            title = (m.get("question", "") or "").lower()
                            if any(k in title for k in TITLE_KEYWORDS):
                                mid = m.get("conditionId") or m.get("id", "")
                                if mid and mid not in seen_ids:
                                    seen_ids.add(mid)
                                    all_markets.append(m)
        except Exception as e:
            logger.error("Error fetching markets for keyword '%s': %s", keyword, e)

    logger.info("Fetched %d unique weather markets from Gamma API", len(all_markets))
    return all_markets


def parse_temperature_from_title(title: str) -> dict:
    """Extract temperature thresholds/buckets from a market title."""
    result = {
        "temp_lower": None,
        "temp_upper": None,
        "threshold": None,
        "is_over": None,
    }

    title_lower = title.lower()

    # Pattern: "X°F or higher" / "above X°" / "over X°"
    over_patterns = [
        r'(\d+)\s*°?\s*f?\s*or\s*(?:higher|more|above)',
        r'(?:above|over|exceed|at\s+least)\s*(\d+)\s*°?\s*f?',
        r'(\d+)\s*°?\s*f?\s*\+',
        r'≥\s*(\d+)',
    ]
    for pat in over_patterns:
        m = re.search(pat, title_lower)
        if m:
            result["threshold"] = float(m.group(1))
            result["is_over"] = True
            return result

    # Pattern: "X°F or lower" / "below X°" / "under X°"
    under_patterns = [
        r'(\d+)\s*°?\s*f?\s*or\s*(?:lower|less|below)',
        r'(?:below|under)\s*(\d+)\s*°?\s*f?',
        r'≤\s*(\d+)',
    ]
    for pat in under_patterns:
        m = re.search(pat, title_lower)
        if m:
            result["threshold"] = float(m.group(1))
            result["is_over"] = False
            return result

    # Pattern: range "X-Y°F" or "between X and Y"
    range_patterns = [
        r'(\d+)\s*[-–]\s*(\d+)\s*°?\s*f?',
        r'between\s*(\d+)\s*(?:°?\s*f?)?\s*and\s*(\d+)\s*°?\s*f?',
    ]
    for pat in range_patterns:
        m = re.search(pat, title_lower)
        if m:
            result["temp_lower"] = float(m.group(1))
            result["temp_upper"] = float(m.group(2))
            return result

    # Pattern: single temperature mentioned
    single_temp = re.search(r'(\d+)\s*°\s*f', title_lower)
    if single_temp:
        result["threshold"] = float(single_temp.group(1))
        result["is_over"] = True
        return result

    return result


def parse_city_from_title(title: str) -> Optional[str]:
    """Extract city name from a market title."""
    title_lower = title.lower()
    city_keywords = get_all_city_keywords()
    city_keywords.sort(key=len, reverse=True)

    for city in city_keywords:
        if city in title_lower:
            return city
    return None


def parse_date_from_title(title: str) -> Optional[datetime]:
    """Extract target date from a market title."""
    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9,
        "oct": 10, "nov": 11, "dec": 12,
    }

    title_lower = title.lower()

    # Full/abbreviated month + day
    month_patterns = [
        r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})',
        r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})',
    ]
    for pat in month_patterns:
        m = re.search(pat, title_lower)
        if m:
            month = month_map[m.group(1)]
            day = int(m.group(2))
            now = datetime.now(timezone.utc)
            year = now.year
            try:
                dt = datetime(year, month, day, tzinfo=timezone.utc)
                if (now - dt).days > 30:
                    dt = datetime(year + 1, month, day, tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue

    # ISO date
    iso_match = re.search(r'(\d{4})-(\d{2})-(\d{2})', title)
    if iso_match:
        try:
            return datetime(int(iso_match.group(1)), int(iso_match.group(2)),
                            int(iso_match.group(3)), tzinfo=timezone.utc)
        except ValueError:
            pass

    # M/D format
    slash_match = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?', title)
    if slash_match:
        month = int(slash_match.group(1))
        day = int(slash_match.group(2))
        year_str = slash_match.group(3)
        year = int(year_str) if year_str else datetime.now(timezone.utc).year
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


def parse_weather_markets(raw_markets: list) -> list:
    """Parse raw Gamma API market data into structured WeatherMarket objects."""
    parsed = []

    for market in raw_markets:
        title = market.get("question", "") or market.get("title", "")
        if not title:
            continue

        city = parse_city_from_title(title)
        if not city:
            continue

        station = lookup_station(city)
        if not station:
            continue

        lat, lon, icao, station_name = station
        target_date = parse_date_from_title(title)
        temp_info = parse_temperature_from_title(title)
        if temp_info["threshold"] is None and temp_info["temp_lower"] is None:
            continue

        # Get price and token info
        outcomes = market.get("outcomes", [])
        prices = market.get("outcomePrices", [])
        clobTokenIds = market.get("clobTokenIds", [])

        # Parse JSON strings if needed
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = []
        if isinstance(clobTokenIds, str):
            try:
                clobTokenIds = json.loads(clobTokenIds)
            except json.JSONDecodeError:
                clobTokenIds = []
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = []

        tokens = market.get("tokens", [])

        if tokens:
            for token in tokens:
                outcome = token.get("outcome", "Yes")
                price = float(token.get("price", 0))
                token_id = token.get("token_id", "")
                parsed.append(WeatherMarket(
                    condition_id=market.get("conditionId", market.get("id", "")),
                    question=title, market_slug=market.get("slug", ""),
                    city=city, station_icao=icao, lat=lat, lon=lon,
                    target_date=target_date,
                    temp_lower=temp_info["temp_lower"], temp_upper=temp_info["temp_upper"],
                    threshold=temp_info["threshold"], is_over=temp_info["is_over"],
                    market_price=price, volume=float(market.get("volume", 0) or 0),
                    liquidity=float(market.get("liquidity", 0) or 0),
                    outcome=outcome, token_id=token_id,
                    end_date=_parse_iso(market.get("endDate")),
                ))
        elif prices:
            for i, price_str in enumerate(prices):
                outcome = outcomes[i] if i < len(outcomes) else ("Yes" if i == 0 else "No")
                price = float(price_str)
                token_id = clobTokenIds[i] if i < len(clobTokenIds) else ""
                parsed.append(WeatherMarket(
                    condition_id=market.get("conditionId", market.get("id", "")),
                    question=title, market_slug=market.get("slug", ""),
                    city=city, station_icao=icao, lat=lat, lon=lon,
                    target_date=target_date,
                    temp_lower=temp_info["temp_lower"], temp_upper=temp_info["temp_upper"],
                    threshold=temp_info["threshold"], is_over=temp_info["is_over"],
                    market_price=price, volume=float(market.get("volume", 0) or 0),
                    liquidity=float(market.get("liquidity", 0) or 0),
                    outcome=outcome, token_id=token_id,
                    end_date=_parse_iso(market.get("endDate")),
                ))
        else:
            price = float(market.get("bestAsk", 0) or 0)
            parsed.append(WeatherMarket(
                condition_id=market.get("conditionId", market.get("id", "")),
                question=title, market_slug=market.get("slug", ""),
                city=city, station_icao=icao, lat=lat, lon=lon,
                target_date=target_date,
                temp_lower=temp_info["temp_lower"], temp_upper=temp_info["temp_upper"],
                threshold=temp_info["threshold"], is_over=temp_info["is_over"],
                market_price=price, volume=float(market.get("volume", 0) or 0),
                liquidity=float(market.get("liquidity", 0) or 0),
                outcome="Yes", token_id="",
                end_date=_parse_iso(market.get("endDate")),
            ))

    logger.info("Parsed %d weather market outcomes from %d raw markets", len(parsed), len(raw_markets))
    return parsed


def _parse_iso(date_str) -> Optional[datetime]:
    """Parse an ISO 8601 date string."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
