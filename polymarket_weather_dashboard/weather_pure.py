"""Pure helpers for the weather dashboard.

These functions are deliberately free of database, network, and Flask
dependencies so they can be unit-tested without booting the server. The
extraction is intentionally surface-only — `server.py` re-binds these names
so existing call-sites keep working.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from typing import Iterable, Optional


def c_to_f(temp_c: float) -> float:
    return temp_c * 9.0 / 5.0 + 32.0


def f_to_c(temp_f: float) -> float:
    return (temp_f - 32.0) * 5.0 / 9.0


# Topics whose titles contain numbers but aren't degree thresholds. Keeping
# these in one place stops parse_temperature from triggering on hurricane
# wind speeds, earthquake magnitudes, etc.
NON_TEMPERATURE_KEYWORDS = (
    "earthquake", "magnitude", "tornado", "hurricane", "landfall",
    "sea ice", "arctic", "volcano", "eruption", "meteor",
    "measles", "cases", "pandemic",
    "snowfall", "rainfall", "inches", "precipitation", "rain", "snow",
)


def make_city_parser(city_keys: Iterable[str], aliases: Optional[dict] = None):
    """Return a `parse_city(title) -> Optional[str]` bound to the given key set.

    `aliases` maps colloquial spellings ("nyc") to the canonical key
    ("new york"). Keys are matched longest-first so "san francisco" is tried
    before "san", and word-boundaries are enforced so "dallas" does not match
    inside "vandals".
    """
    aliases = aliases or {}
    all_keys = sorted(set(city_keys) | set(aliases.keys()), key=len, reverse=True)
    patterns = [(k, re.compile(r"\b" + re.escape(k) + r"\b")) for k in all_keys]

    def parse_city(title: str) -> Optional[str]:
        if not title:
            return None
        tl = title.lower()
        for key, pat in patterns:
            if pat.search(tl):
                return aliases.get(key, key)
        return None

    return parse_city


def parse_temperature(title: str) -> dict:
    """Parse a temperature threshold from a market title.

    Returns a dict shaped {threshold, is_over, temp_lower, temp_upper, unit}.
    Any None field means "not present"; an all-None result means the title
    isn't a temperature market we can score.
    """
    result = {"temp_lower": None, "temp_upper": None, "threshold": None,
              "is_over": None, "unit": "F"}
    if not title:
        return result
    tl = title.lower()

    if any(k in tl for k in NON_TEMPERATURE_KEYWORDS):
        return result

    # Celsius range: "between 1.5 and 1.7°C"
    m = re.search(r"between\s*([\d.]+)\s*[º°]?\s*c?\s*and\s*([\d.]+)\s*[º°]?\s*c", tl)
    if m:
        result["temp_lower"] = float(m.group(1))
        result["temp_upper"] = float(m.group(2))
        result["unit"] = "C"
        return result

    m = re.search(r"(?:more than|above|over|exceed|at least|greater than)\s*([\d.]+)\s*[º°]\s*c", tl)
    if m:
        result["threshold"] = float(m.group(1))
        result["is_over"] = True
        result["unit"] = "C"
        return result

    m = re.search(r"(?:less than|below|under)\s*([\d.]+)\s*[º°]\s*c", tl)
    if m:
        result["threshold"] = float(m.group(1))
        result["is_over"] = False
        result["unit"] = "C"
        return result

    # Polymarket slug-style: "1pt20c"
    m = re.search(r"between\s*(\d+)pt(\d+)[º°]?c?\s*and\s*(\d+)pt(\d+)[º°]?c", tl)
    if m:
        result["temp_lower"] = float(f"{m.group(1)}.{m.group(2)}")
        result["temp_upper"] = float(f"{m.group(3)}.{m.group(4)}")
        result["unit"] = "C"
        return result

    # Fahrenheit "above"
    for pat in [r"(\d+)\s*°?\s*f?\s*or\s*(?:higher|more|above)",
                r"(?:above|over|exceed|at\s+least)\s*(\d+)\s*°?\s*f",
                r"(\d+)\s*°?\s*f?\s*\+", r"≥\s*(\d+)"]:
        m = re.search(pat, tl)
        if m:
            result["threshold"] = float(m.group(1))
            result["is_over"] = True
            return result

    # Fahrenheit "below"
    for pat in [r"(\d+)\s*°?\s*f?\s*or\s*(?:lower|less|below)",
                r"(?:below|under)\s*(\d+)\s*°?\s*f", r"≤\s*(\d+)"]:
        m = re.search(pat, tl)
        if m:
            result["threshold"] = float(m.group(1))
            result["is_over"] = False
            return result

    # Fahrenheit range
    for pat in [r"(\d+)\s*[-–]\s*(\d+)\s*°?\s*f",
                r"between\s*(\d+)\s*(?:°?\s*f?)?\s*and\s*(\d+)\s*°?\s*f"]:
        m = re.search(pat, tl)
        if m:
            result["temp_lower"] = float(m.group(1))
            result["temp_upper"] = float(m.group(2))
            return result

    single = re.search(r"(\d+)\s*°\s*f", tl)
    if single:
        result["threshold"] = float(single.group(1))
        result["is_over"] = True
        return result

    return result


_MONTH_NUMBER = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date(title: str, today: Optional[datetime] = None) -> Optional[str]:
    """Extract a YYYY-MM-DD target date from a market title.

    `today` is injected for deterministic testing. If a month-day pair is
    parsed but the implied year would put the date >30 days in the past, we
    bump to next year (markets resolve in the future).
    """
    if not title:
        return None
    today = today or datetime.now(timezone.utc)
    tl = title.lower()

    for pat in [r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b",
                r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})\b"]:
        m = re.search(pat, tl)
        if m:
            month = _MONTH_NUMBER[m.group(1)]
            day = int(m.group(2))
            try:
                dt = datetime(today.year, month, day, tzinfo=timezone.utc)
                if (today - dt).days > 30:
                    dt = datetime(today.year + 1, month, day, tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

    full_months = {k: v for k, v in _MONTH_NUMBER.items() if len(k) > 3 or k == "may"}
    for name, num in full_months.items():
        if re.search(r"\b" + name + r"\b", tl):
            year_m = re.search(r"(20\d{2})", title)
            year = int(year_m.group(1)) if year_m else today.year
            return f"{year}-{num:02d}-15"

    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", title)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
    return None


def parse_threshold_for_resolution(question: str) -> tuple:
    """Parse a market question into (kind, v1, v2).

    `kind` is one of 'above', 'below', 'between', or None. Used by the
    backtest to resolve a market against an observed temperature without
    pulling in the full parse_temperature heuristics.
    """
    if not question:
        return (None, None, None)
    q = question.lower()

    m = re.search(r"between\s+(\d+)[–\-]\s*(\d+)", q)
    if m:
        return ("between", int(m.group(1)), int(m.group(2)))
    m = re.search(r"between\s+(\d+)\s+and\s+(\d+)", q)
    if m:
        return ("between", int(m.group(1)), int(m.group(2)))

    m = re.search(r"(\d+)\s*°?\s*f?\s+or\s+(higher|above|more)", q)
    if m:
        return ("above", int(m.group(1)), None)
    m = re.search(r"(above|over|at\s+least|exceed)\s+(\d+)", q)
    if m:
        return ("above", int(m.group(2)), None)

    m = re.search(r"(\d+)\s*°?\s*f?\s+or\s+(below|lower|less)", q)
    if m:
        return ("below", int(m.group(1)), None)
    m = re.search(r"(below|under|at\s+most)\s+(\d+)", q)
    if m:
        return ("below", int(m.group(2)), None)

    return (None, None, None)


def resolve_market(observed_high_f: float, threshold: tuple) -> Optional[bool]:
    """Given an observed high (°F) and a parsed threshold tuple, return
    True if YES wins, False if NO wins, None if unresolvable."""
    kind, v1, v2 = threshold
    if observed_high_f is None or kind is None:
        return None
    if kind == "above":
        return observed_high_f >= v1
    if kind == "below":
        return observed_high_f <= v1
    if kind == "between":
        return v1 <= observed_high_f <= v2
    return None


def safe_clamp_probability(p) -> Optional[float]:
    """Clamp to [0.01, 0.99]. NaN/inf return None — the caller should treat
    that as "we couldn't compute a probability" rather than a confident bet."""
    try:
        pf = float(p)
    except (TypeError, ValueError):
        return None
    import math
    if math.isnan(pf) or math.isinf(pf):
        return None
    return max(0.01, min(0.99, pf))


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in statute miles."""
    import math
    R = 3959.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def empirical_quantile(values, q: float) -> Optional[float]:
    """Return the q-th empirical quantile (0 <= q <= 1) of values, or None
    if there's not enough data. Linear interpolation between order
    statistics — same convention as numpy's default."""
    if not values or not (0.0 <= q <= 1.0):
        return None
    s = sorted(float(v) for v in values if v is not None)
    n = len(s)
    if n == 0:
        return None
    if n == 1:
        return s[0]
    idx = q * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def empirical_cdf_above(values, threshold: float) -> Optional[float]:
    """Empirical P(X >= threshold) from a sample. Includes equality."""
    if not values:
        return None
    n = sum(1 for v in values if v is not None)
    if n == 0:
        return None
    above = sum(1 for v in values if v is not None and v >= threshold)
    return above / n


def empirical_cdf_below(values, threshold: float) -> Optional[float]:
    """Empirical P(X <= threshold) from a sample. Includes equality."""
    if not values:
        return None
    n = sum(1 for v in values if v is not None)
    if n == 0:
        return None
    below = sum(1 for v in values if v is not None and v <= threshold)
    return below / n


def empirical_cdf_between(values, lower: float, upper: float) -> Optional[float]:
    """Empirical P(lower <= X <= upper)."""
    if not values:
        return None
    n = sum(1 for v in values if v is not None)
    if n == 0:
        return None
    inside = sum(1 for v in values if v is not None and lower <= v <= upper)
    return inside / n


def standard_deviation(values) -> Optional[float]:
    """Sample stdev, with safe fallback for n<2."""
    cleaned = [float(v) for v in values if v is not None]
    if len(cleaned) < 2:
        return None
    return statistics.stdev(cleaned)
