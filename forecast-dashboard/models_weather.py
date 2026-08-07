"""Weather model — parses daily-high temperature markets (Polymarket titles,
Kalshi KXHIGH tickers), fetches Open-Meteo GFS ensemble forecasts, and emits
model probability rows for db.insert_model_prob.

Vendored (never imported) from polymarket_weather_bot: city_stations.py,
gamma_client.py parsers, kalshi_markets.py ticker parser, weather_client.py
Open-Meteo fetchers, edge_calculator.py probability math (Laplace estimator
(m+1)/(n+2) for ensemble counting; Gaussian via math.erf with [0.01, 0.99]
clamp — no scipy). The bot's B/T ticker semantics are NOT vendored: against
the live catalog B is a range bracket (B95.5 = "95° to 96°") and T is either
tail (">96°" and "<89°" both ship as T), so the ticker supplies only station
and date; threshold/range and direction come from the question text.

Row contract:
    {
        "market_uid": "<venue>:<venue_id>",
        "source": "weather",
        "model_prob": float,             # 0..1
        "prob_method": "ensemble" | "gaussian",
        "detail": str,                   # JSON: station/date/threshold/members
    }

Unparseable markets are silently skipped.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
from datetime import date, datetime, timezone

import httpx

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

_MIN_ENSEMBLE_MEMBERS = 10
_MAX_STATIONS = 15

# City name variants → (latitude, longitude, ICAO code, display name).
# Polymarket resolves on airport station readings, not city centers.
STATION_MAP: dict = {
    "new york": (40.7772, -73.8726, "KLGA", "LaGuardia Airport"),
    "nyc": (40.7772, -73.8726, "KLGA", "LaGuardia Airport"),
    "chicago": (41.9742, -87.9073, "KORD", "O'Hare International"),
    "dallas": (32.8471, -96.8518, "KDAL", "Dallas Love Field"),
    "miami": (25.7959, -80.2870, "KMIA", "Miami International"),
    "los angeles": (33.9425, -118.4081, "KLAX", "LAX"),
    "la": (33.9425, -118.4081, "KLAX", "LAX"),
    "denver": (39.8561, -104.6737, "KDEN", "Denver International"),
    "london": (51.5053, -0.0553, "EGLC", "London City Airport"),
    "paris": (48.7233, 2.3794, "LFPO", "Paris-Orly"),
    "tokyo": (35.5533, 139.7811, "RJTT", "Haneda Airport"),
    "seoul": (37.5586, 126.7906, "RKSS", "Gimpo International"),
    "sydney": (-33.9461, 151.1772, "YSSY", "Sydney Airport"),
}

CITY_ALIASES: dict = {
    "new york city": "new york",
    "manhattan": "new york",
    "brooklyn": "new york",
    "chi-town": "chicago",
    "chi": "chicago",
    "l.a.": "la",
    "l.a": "la",
    "dfw": "dallas",
    "fort worth": "dallas",
}

# Kalshi KXHIGH series → city key in STATION_MAP
SERIES_TO_CITY: dict = {
    "KXHIGHNY": "new york",
    "KXHIGHCHI": "chicago",
    "KXHIGHMIA": "miami",
    "KXHIGHLAX": "los angeles",
    "KXHIGHDEN": "denver",
}

_MONTH_ABBR = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

# Markets arrive unfiltered here (unlike the bot, which pre-filters by weather
# tags), so require an explicit temperature hint before trusting the loose
# threshold regexes — "over 20 points" must not parse as a temperature.
_WEATHER_HINT_RX = re.compile(
    r"\btemperature\b|\btemp\b|°|\bdegrees?\b|\bhottest\b|\bcoldest\b|\bheat\s*wave\b|\bfahrenheit\b",
    re.I,
)


def lookup_station(city_name: str) -> tuple | None:
    key = city_name.strip().lower()
    key = CITY_ALIASES.get(key, key)
    return STATION_MAP.get(key)


def _all_city_keywords() -> list:
    return sorted(set(list(STATION_MAP.keys()) + list(CITY_ALIASES.keys())))


# ── question parsing ─────────────────────────────────────────────────────────


def parse_temperature_from_title(title: str) -> dict:
    result = {"temp_lower": None, "temp_upper": None, "threshold": None, "is_over": None}
    title_lower = title.lower()

    over_patterns = [
        r"(\d+)\s*°?\s*f?\s*or\s*(?:higher|more|above)",
        r"(?:above|over|exceed|at\s+least)\s*(\d+)\s*°?\s*f?",
        r"(\d+)\s*°?\s*f?\s*\+",
        r"≥\s*(\d+)",
    ]
    for pat in over_patterns:
        m = re.search(pat, title_lower)
        if m:
            result["threshold"] = float(m.group(1))
            result["is_over"] = True
            return result

    under_patterns = [
        r"(\d+)\s*°?\s*f?\s*or\s*(?:lower|less|below)",
        r"(?:below|under)\s*(\d+)\s*°?\s*f?",
        r"≤\s*(\d+)",
    ]
    for pat in under_patterns:
        m = re.search(pat, title_lower)
        if m:
            result["threshold"] = float(m.group(1))
            result["is_over"] = False
            return result

    range_patterns = [
        r"(\d+)\s*[-–]\s*(\d+)\s*°?\s*f?",
        r"between\s*(\d+)\s*(?:°?\s*f?)?\s*and\s*(\d+)\s*°?\s*f?",
        r"(\d+)\s*°\s*f?\s*to\s*(\d+)\s*°?\s*f?",
    ]
    for pat in range_patterns:
        m = re.search(pat, title_lower)
        if m:
            result["temp_lower"] = float(m.group(1))
            result["temp_upper"] = float(m.group(2))
            return result

    single_temp = re.search(r"(\d+)\s*°\s*f", title_lower)
    if single_temp:
        result["threshold"] = float(single_temp.group(1))
        result["is_over"] = True
        return result

    return result


def parse_city_from_title(title: str) -> str | None:
    title_lower = title.lower()
    city_keywords = _all_city_keywords()
    city_keywords.sort(key=len, reverse=True)
    for city in city_keywords:
        if re.search(r"\b" + re.escape(city) + r"\b", title_lower):
            return CITY_ALIASES.get(city, city)
    return None


def parse_date_from_title(title: str) -> date | None:
    month_map = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    title_lower = title.lower()

    # \b keeps "may" from matching inside "dismay" and "mar" inside "market".
    month_patterns = [
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b",
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})\b",
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
                return dt.date()
            except ValueError:
                continue

    iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", title)
    if iso_match:
        try:
            return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        except ValueError:
            pass

    slash_match = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", title)
    if slash_match:
        month = int(slash_match.group(1))
        day = int(slash_match.group(2))
        year_str = slash_match.group(3)
        year = int(year_str) if year_str else datetime.now(timezone.utc).year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            pass

    return None


def parse_kalshi_ticker(ticker: str) -> dict | None:
    """KXHIGHNY-26MAR01-B45.5 → city + date only. The B/T suffix does not
    encode the event: B is a range bracket (B95.5 = "95° to 96°") and T is
    either tail (">96°" and "<89°" are both T), so threshold and direction
    must be read from the question text."""
    match = re.match(r"^([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-[BT][\d.]+$", ticker or "")
    if not match:
        return None
    series = match.group(1)
    city = SERIES_TO_CITY.get(series)
    if not city:
        return None
    month = _MONTH_ABBR.get(match.group(3))
    if not month:
        return None
    try:
        target = date(2000 + int(match.group(2)), month, int(match.group(4)))
    except ValueError:
        return None
    return {"city": city, "target_date": target}


def parse_weather_market(question: str, venue: str | None = None, venue_id: str | None = None) -> dict | None:
    """Parse a market into {city, icao, lat, lon, station_name, target_date,
    threshold, temp_lower, temp_upper, is_over}. None if not a parseable
    temperature market."""
    if venue == "kalshi" and venue_id:
        parsed = parse_kalshi_ticker(venue_id)
        if parsed:
            station = lookup_station(parsed["city"])
            if station:
                temp_info = parse_temperature_from_title(question or "")
                if temp_info["threshold"] is None and temp_info["temp_lower"] is None:
                    return None
                lat, lon, icao, name = station
                return {
                    "city": parsed["city"],
                    "icao": icao,
                    "lat": lat,
                    "lon": lon,
                    "station_name": name,
                    "target_date": parsed["target_date"],
                    "threshold": temp_info["threshold"],
                    "temp_lower": temp_info["temp_lower"],
                    "temp_upper": temp_info["temp_upper"],
                    "is_over": temp_info["is_over"],
                }

    title = question or ""
    if not _WEATHER_HINT_RX.search(title):
        return None
    city = parse_city_from_title(title)
    if not city:
        return None
    station = lookup_station(city)
    if not station:
        return None
    temp_info = parse_temperature_from_title(title)
    if temp_info["threshold"] is None and temp_info["temp_lower"] is None:
        return None
    lat, lon, icao, name = station
    return {
        "city": city,
        "icao": icao,
        "lat": lat,
        "lon": lon,
        "station_name": name,
        "target_date": parse_date_from_title(title),
        "threshold": temp_info["threshold"],
        "temp_lower": temp_info["temp_lower"],
        "temp_upper": temp_info["temp_upper"],
        "is_over": temp_info["is_over"],
    }


# ── probability math ─────────────────────────────────────────────────────────


def _phi(x: float, mean: float, std: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mean) / (std * math.sqrt(2.0))))


def gaussian_probability(mean: float, std: float, parsed: dict) -> float:
    std = max(std, 0.1)
    if parsed.get("threshold") is not None:
        p_below = _phi(parsed["threshold"], mean, std)
        prob = 1.0 - p_below if parsed.get("is_over") else p_below
    elif parsed.get("temp_lower") is not None and parsed.get("temp_upper") is not None:
        prob = _phi(parsed["temp_upper"] + 0.5, mean, std) - _phi(parsed["temp_lower"] - 0.5, mean, std)
    else:
        return 0.0
    return max(0.01, min(0.99, prob))


def laplace_probability(members: list, parsed: dict) -> float:
    """Laplace estimator (m+1)/(n+2): a unanimous 31-member ensemble yields
    ~0.97, never 1.0, so correctly-extreme-priced markets don't show fake edge."""
    n = len(members)
    if parsed.get("threshold") is not None:
        if parsed.get("is_over"):
            count = sum(1 for t in members if t > parsed["threshold"])
        else:
            count = sum(1 for t in members if t < parsed["threshold"])
    elif parsed.get("temp_lower") is not None and parsed.get("temp_upper") is not None:
        lo = parsed["temp_lower"] - 0.5
        hi = parsed["temp_upper"] + 0.5
        count = sum(1 for t in members if lo <= t <= hi)
    else:
        return 0.0
    return (count + 1) / (n + 2)


# ── Open-Meteo fetch ─────────────────────────────────────────────────────────


async def _fetch_forecast(client: httpx.AsyncClient, lat: float, lon: float, target_date: date) -> dict | None:
    """GFS ensemble daily-max temps, deterministic fallback.

    Returns {members, mean, std, source} or None."""
    date_str = target_date.isoformat()
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "start_date": date_str,
        "end_date": date_str,
        "models": "gfs_seamless",
    }
    try:
        resp = await client.get(OPEN_METEO_ENSEMBLE_URL, params=params)
        if resp.status_code == 200:
            daily = resp.json().get("daily", {})
            members = []
            for key, values in daily.items():
                if key.startswith("temperature_2m_max") and values:
                    members.extend(float(v) for v in values if v is not None)
            if members:
                std = statistics.stdev(members) if len(members) > 1 else 3.0
                return {
                    "members": members,
                    "mean": statistics.mean(members),
                    "std": max(std, 2.0),
                    "source": "open-meteo-ensemble",
                }
        else:
            log.warning("Open-Meteo ensemble returned %d", resp.status_code)
    except Exception as e:
        log.warning("Open-Meteo ensemble error: %s", e)

    params.pop("models", None)
    try:
        resp = await client.get(OPEN_METEO_URL, params=params)
        if resp.status_code != 200:
            return None
        temps = resp.json().get("daily", {}).get("temperature_2m_max", [])
        if not temps or temps[0] is None:
            return None
        mean = float(temps[0])
        target_dt = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
        hours_ahead = (target_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        std = min(2.5 + 0.03 * max(0, hours_ahead), 6.0)
        return {"members": [mean], "mean": mean, "std": std, "source": "open-meteo-deterministic"}
    except Exception as e:
        log.warning("Open-Meteo deterministic error: %s", e)
        return None


# ── entry point ──────────────────────────────────────────────────────────────


def _date_from_iso(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


async def compute(markets: list[dict]) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    candidates = []
    for m in markets:
        parsed = parse_weather_market(m.get("question") or "", m.get("venue"), m.get("venue_id"))
        if not parsed:
            continue
        target = parsed["target_date"] or _date_from_iso(m.get("end_date"))
        if not target or target < today:
            continue
        candidates.append((m, parsed, target))
    if not candidates:
        return []

    rows: list[dict] = []
    cache: dict = {}
    stations_used: set = set()
    async with httpx.AsyncClient(timeout=15.0) as client:
        for m, parsed, target in candidates:
            key = (parsed["icao"], target.isoformat())
            if key not in cache:
                if parsed["icao"] not in stations_used and len(stations_used) >= _MAX_STATIONS:
                    continue
                stations_used.add(parsed["icao"])
                cache[key] = await _fetch_forecast(client, parsed["lat"], parsed["lon"], target)
            forecast = cache[key]
            if not forecast:
                continue

            members = forecast["members"]
            if len(members) >= _MIN_ENSEMBLE_MEMBERS:
                prob = laplace_probability(members, parsed)
                method = "ensemble"
            else:
                prob = gaussian_probability(forecast["mean"], forecast["std"], parsed)
                method = "gaussian"

            uid = m.get("uid") or f"{m.get('venue')}:{m.get('venue_id')}"
            rows.append(
                {
                    "market_uid": uid,
                    "source": "weather",
                    "model_prob": round(prob, 4),
                    "prob_method": method,
                    "detail": json.dumps(
                        {
                            "station": parsed["icao"],
                            "city": parsed["city"],
                            "date": target.isoformat(),
                            "threshold": parsed["threshold"],
                            "temp_lower": parsed["temp_lower"],
                            "temp_upper": parsed["temp_upper"],
                            "is_over": parsed["is_over"],
                            "members": len(members),
                            "mean": round(forecast["mean"], 2),
                            "std": round(forecast["std"], 2),
                            "forecast_source": forecast["source"],
                        }
                    ),
                }
            )
    return rows
