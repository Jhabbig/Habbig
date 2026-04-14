"""City to airport weather station mapping.

CRITICAL: Polymarket resolves weather markets on airport station readings,
not city center observations. This mapping must match Polymarket's resolution source.
"""

from __future__ import annotations

from typing import Optional, List, Tuple

# City name variants → (latitude, longitude, ICAO code, display name)
STATION_MAP: dict = {
    # US stations
    "new york":      (40.7772, -73.8726, "KLGA", "LaGuardia Airport"),
    "nyc":           (40.7772, -73.8726, "KLGA", "LaGuardia Airport"),
    "chicago":       (41.9742, -87.9073, "KORD", "O'Hare International"),
    "dallas":        (32.8471, -96.8518, "KDAL", "Dallas Love Field"),
    "miami":         (25.7959, -80.2870, "KMIA", "Miami International"),
    "los angeles":   (33.9425, -118.4081, "KLAX", "LAX"),
    "la":            (33.9425, -118.4081, "KLAX", "LAX"),
    "denver":        (39.8561, -104.6737, "KDEN", "Denver International"),
    # International stations
    "london":        (51.5053, -0.0553,  "EGLC", "London City Airport"),
    "paris":         (48.7233, 2.3794,   "LFPO", "Paris-Orly"),
    "tokyo":         (35.5533, 139.7811, "RJTT", "Haneda Airport"),
    "seoul":         (37.5586, 126.7906, "RKSS", "Gimpo International"),
    "sydney":        (-33.9461, 151.1772, "YSSY", "Sydney Airport"),
}

# Additional aliases
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


def lookup_station(city_name: str) -> Optional[tuple]:
    """Look up weather station for a city name (case-insensitive).

    Returns (lat, lon, icao_code, station_name) or None if not found.
    """
    key = city_name.strip().lower()
    key = CITY_ALIASES.get(key, key)
    return STATION_MAP.get(key)


def get_all_city_keywords() -> list:
    """Return all known city names and aliases for market title matching."""
    all_keys = list(STATION_MAP.keys()) + list(CITY_ALIASES.keys())
    return sorted(set(all_keys))
