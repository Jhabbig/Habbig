"""Weather model — parses daily-high AND daily-low temperature markets
(Polymarket titles, Kalshi KXHIGH tickers), fetches Open-Meteo multi-model
ensemble forecasts (GFS + ECMWF + ICON + GEM super-ensemble, both
temperature_2m_max and _min in one request, station-local calendar days via
timezone=auto), and emits model probability rows for db.insert_model_prob.

Probability engine: Gaussian kernel dressing over the pooled members —
P(bracket [lo,hi]) = mean over members m of
Phi((hi+0.5 - (t_m + bias)) / sigma_k) - Phi((lo-0.5 - (t_m + bias)) / sigma_k),
thresholds one-tailed with the same half-degree continuity correction.
Per-(station, kind) bias and the kernel width sigma_k are learned from our
own resolution ledger: winning bracket rows in the calibration table pin the
actual daily extreme to a midpoint, bias = mean(actual - forecast_mean)
shrunk by n/(n+5) and capped at ±4°F, sigma_k = cross-day residual std
clamped to [1.2, 3.0] (default 1.8 until ≥2 resolved days exist). For
markets targeting today (station-local), METAR observations constrain the
distribution: highs settle as max(running_max, T) (mass below the observed
running max collapses onto it), lows as min(running_min, T) (mass above the
running min collapses onto it). Bias/sigma use outcomes only — never
market prices.

Vendored (never imported) from polymarket_weather_bot: city_stations.py,
gamma_client.py parsers, kalshi_markets.py ticker parser. The bot's B/T
ticker semantics are NOT vendored: against the live catalog B is a range
bracket (B95.5 = "95° to 96°") and T is either tail (">96°" and "<89°" both
ship as T), so the ticker supplies only station and date; threshold/range
and direction come from the question text.

Row contract:
    {
        "market_uid": "<venue>:<venue_id>",
        "source": "weather",
        "model_prob": float,             # 0..1
        "prob_method": "ensemble_dressed" | "ensemble_intraday" | "gaussian" | "gaussian_intraday",
        "detail": str,                   # JSON: station/date/threshold/members
    }

Unparseable markets are silently skipped.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import statistics
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx

import db

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
METAR_URL = "https://aviationweather.gov/api/data/metar"

ENSEMBLE_MODELS = "gfs_seamless,ecmwf_ifs025,icon_seamless,gem_global"

_MIN_ENSEMBLE_MEMBERS = 10
_MAX_STATIONS = 15
_DEFAULT_SIGMA_K = 1.8
_SIGMA_K_RANGE = (1.2, 3.0)
_BIAS_SHRINK_N = 5
_BIAS_CAP = 4.0

# Open-Meteo rewrites requested model aliases into resolved IDs in the daily
# keys (gfs_seamless → ncep_gefs_seamless), and control members carry no
# memberNN suffix — match both shapes or 4 of 143 members silently vanish.
_MEMBER_KEY_RX = re.compile(r"^temperature_2m_(?P<var>max|min)(?:_member\d+)?(?:_(?P<model>[a-z].*))?$")

# City name variants → (latitude, longitude, ICAO code, display name, IANA tz).
# Polymarket resolves on airport station readings, not city centers.
STATION_MAP: dict = {
    "new york": (40.7772, -73.8726, "KLGA", "LaGuardia Airport", "America/New_York"),
    "nyc": (40.7772, -73.8726, "KLGA", "LaGuardia Airport", "America/New_York"),
    "chicago": (41.9742, -87.9073, "KORD", "O'Hare International", "America/Chicago"),
    "dallas": (32.8471, -96.8518, "KDAL", "Dallas Love Field", "America/Chicago"),
    "miami": (25.7959, -80.2870, "KMIA", "Miami International", "America/New_York"),
    "los angeles": (33.9425, -118.4081, "KLAX", "LAX", "America/Los_Angeles"),
    "la": (33.9425, -118.4081, "KLAX", "LAX", "America/Los_Angeles"),
    "denver": (39.8561, -104.6737, "KDEN", "Denver International", "America/Denver"),
    "london": (51.5053, -0.0553, "EGLC", "London City Airport", "Europe/London"),
    "paris": (48.7233, 2.3794, "LFPO", "Paris-Orly", "Europe/Paris"),
    "tokyo": (35.5533, 139.7811, "RJTT", "Haneda Airport", "Asia/Tokyo"),
    "seoul": (37.5586, 126.7906, "RKSS", "Gimpo International", "Asia/Seoul"),
    "sydney": (-33.9461, 151.1772, "YSSY", "Sydney Airport", "Australia/Sydney"),
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

# Daily-low markets ("lowest temperature in Miami…") get the temperature_2m_min
# ensemble and the running-min intraday constraint; everything else is a high.
# Legacy ledger rows scored before kind-awareness carry a high forecast against
# a low question — learn_station_bias skips them via the detail-vs-question
# kind mismatch.
_LOW_TEMP_RX = re.compile(
    r"\b(?:low|lowest|min|minimum|coldest)\s+temp(?:erature)?s?\b|\b(?:overnight|daily|tonight'?s)\s+low\b",
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
    """Parse a market into {city, icao, lat, lon, station_name, tz, kind,
    target_date, threshold, temp_lower, temp_upper, is_over}. kind is
    'high' or 'low'. None if not a parseable temperature market."""
    kind = "low" if _LOW_TEMP_RX.search(question or "") else "high"
    if venue == "kalshi" and venue_id:
        parsed = parse_kalshi_ticker(venue_id)
        if parsed:
            station = lookup_station(parsed["city"])
            if station:
                temp_info = parse_temperature_from_title(question or "")
                if temp_info["threshold"] is None and temp_info["temp_lower"] is None:
                    return None
                lat, lon, icao, name, tz = station
                return {
                    "city": parsed["city"],
                    "icao": icao,
                    "lat": lat,
                    "lon": lon,
                    "station_name": name,
                    "tz": tz,
                    "kind": kind,
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
    lat, lon, icao, name, tz = station
    return {
        "city": city,
        "icao": icao,
        "lat": lat,
        "lon": lon,
        "station_name": name,
        "tz": tz,
        "kind": kind,
        "target_date": parse_date_from_title(title),
        "threshold": temp_info["threshold"],
        "temp_lower": temp_info["temp_lower"],
        "temp_upper": temp_info["temp_upper"],
        "is_over": temp_info["is_over"],
    }


# ── probability math ─────────────────────────────────────────────────────────


def _phi(x: float, mean: float, std: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mean) / (std * math.sqrt(2.0))))


def gaussian_probability(mean: float, std: float, parsed: dict, runmax: float | None = None, runmin: float | None = None) -> float:
    std = max(std, 0.1)

    def cdf(x: float) -> float:
        if runmax is not None and x < runmax:
            return 0.0
        if runmin is not None and x >= runmin:
            return 1.0
        return _phi(x, mean, std)

    # Same edge convention as dressed_probability: thresholds are inclusive,
    # so >=t is 1-CDF(t-0.5) and <=t is CDF(t+0.5) — the tails share edges
    # with the brackets and a full ladder sums to 1.
    if parsed.get("threshold") is not None:
        t = parsed["threshold"]
        prob = 1.0 - cdf(t - 0.5) if parsed.get("is_over") else cdf(t + 0.5)
    elif parsed.get("temp_lower") is not None and parsed.get("temp_upper") is not None:
        prob = cdf(parsed["temp_upper"] + 0.5) - cdf(parsed["temp_lower"] - 0.5)
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


def dressed_probability(members: list, parsed: dict, sigma_k: float, bias: float = 0.0, runmax: float | None = None, runmin: float | None = None) -> float:
    """Kernel dressing: each bias-adjusted member becomes a Gaussian of width
    sigma_k; probabilities are the mixture CDF evaluated at half-degree
    bracket edges (venues resolve on integer-rounded extremes). With a METAR
    running max R, the final high is max(R, T): CDF is 0 below R and the
    mass below R collapses onto it. With a running min R (low markets), the
    final low is min(R, T): CDF is 1 at and above R, mass above collapses
    onto it."""
    if not members:
        return 0.0
    sigma_k = max(sigma_k, 0.1)
    adj = [t + bias for t in members]

    def cdf(x: float) -> float:
        if runmax is not None and x < runmax:
            return 0.0
        if runmin is not None and x >= runmin:
            return 1.0
        return sum(_phi(x, t, sigma_k) for t in adj) / len(adj)

    # Parsed thresholds are inclusive ("89° or above" → threshold 89), so the
    # tails share edges with the brackets: >=t is 1-CDF(t-0.5), <=t is CDF(t+0.5).
    if parsed.get("threshold") is not None:
        t = parsed["threshold"]
        prob = 1.0 - cdf(t - 0.5) if parsed.get("is_over") else cdf(t + 0.5)
    elif parsed.get("temp_lower") is not None and parsed.get("temp_upper") is not None:
        prob = cdf(parsed["temp_upper"] + 0.5) - cdf(parsed["temp_lower"] - 0.5)
    else:
        return 0.0
    return max(0.01, min(0.99, prob))


# ── ledger-learned bias & kernel width ───────────────────────────────────────


def learn_station_bias(conn: sqlite3.Connection) -> dict:
    """Recover actual daily extremes from resolved bracket markets: a winning
    bracket (outcome=1, temp_lower/temp_upper set) pins the high or low to
    its midpoint. Tails are ignored. Rows whose recorded forecast kind does
    not match the question's kind are skipped — legacy low markets were
    scored against high forecasts before kind-awareness, and their means
    would poison the fit. Two winning brackets for one station-day-kind are
    intersected — venues share integer endpoints (Kalshi "83-84" and
    Polymarket "84-85" both pay on a high of 84) — and only truly disjoint
    winners drop the day as contradictory.
    Returns {"bias": {(icao, kind): shrunk capped bias}, "residual_std", "days"}."""
    out: dict = {"bias": {}, "residual_std": None, "days": 0}
    try:
        rows = conn.execute(
            """SELECT c.market_uid, MAX(c.outcome) AS outcome,
                      (SELECT mp.detail FROM model_probs mp
                       WHERE mp.market_uid = c.market_uid AND mp.source = 'weather'
                       ORDER BY mp.ts DESC LIMIT 1) AS detail,
                      (SELECT mk.question FROM markets mk
                       WHERE mk.uid = c.market_uid) AS question
               FROM calibration c
               WHERE c.source = 'weather' AND c.outcome IS NOT NULL
               GROUP BY c.market_uid"""
        ).fetchall()
    except sqlite3.Error as e:
        log.warning("bias ledger query failed: %s", e)
        return out

    days: dict = {}
    contradicted: set = set()
    for r in rows:
        if r["outcome"] != 1 or not r["detail"]:
            continue
        try:
            d = json.loads(r["detail"])
        except ValueError:
            continue
        if d.get("temp_lower") is None or d.get("temp_upper") is None:
            continue
        question_kind = "low" if _LOW_TEMP_RX.search(r["question"] or "") else "high"
        if d.get("kind", "high") != question_kind:
            continue
        station, day, mean = d.get("station"), d.get("date"), d.get("mean")
        if not station or not day or mean is None:
            continue
        lo, hi = float(d["temp_lower"]), float(d["temp_upper"])
        key = (station, day, question_kind)
        prev = days.get(key)
        if prev is None:
            days[key] = [lo, hi, [float(mean)]]
        else:
            new_lo, new_hi = max(prev[0], lo), min(prev[1], hi)
            if new_lo > new_hi:
                contradicted.add(key)
            else:
                prev[0], prev[1] = new_lo, new_hi
                prev[2].append(float(mean))
    for key in contradicted:
        days.pop(key, None)
    if not days:
        return out

    errors_by_key: dict = {}
    all_errors: list = []
    for (station, _, kind), (lo, hi, means) in days.items():
        err = (lo + hi) / 2.0 - sum(means) / len(means)
        errors_by_key.setdefault((station, kind), []).append(err)
        all_errors.append(err)
    out["days"] = len(days)
    if len(all_errors) >= 2:
        out["residual_std"] = statistics.stdev(all_errors)
    for key, errs in errors_by_key.items():
        n = len(errs)
        shrunk = (sum(errs) / n) * n / (n + _BIAS_SHRINK_N)
        out["bias"][key] = max(-_BIAS_CAP, min(_BIAS_CAP, shrunk))
    return out


def _sigma_from_residual(residual_std: float | None) -> float:
    if residual_std is None:
        return _DEFAULT_SIGMA_K
    lo, hi = _SIGMA_K_RANGE
    return min(hi, max(lo, residual_std))


def _load_ledger(conn: sqlite3.Connection | None) -> dict:
    own = conn is None
    if own:
        try:
            conn = db.get_conn()
        except Exception as e:
            log.warning("ledger unavailable; skipping bias correction: %s", e)
            return {"bias": {}, "residual_std": None, "days": 0}
    try:
        return learn_station_bias(conn)
    finally:
        if own:
            conn.close()


# ── intraday METAR constraint ────────────────────────────────────────────────


async def _fetch_metar_extremes(client: httpx.AsyncClient, stations: dict) -> dict:
    """stations: {icao: (tz_name, target_date)} → {icao: {"high": running max
    °F, "low": running min °F}} over observations falling on the
    station-local target day. METAR temp is Celsius (decimal, from the
    T-group)."""
    params = {"ids": ",".join(sorted(stations)), "format": "json", "hours": 36}
    resp = await client.get(METAR_URL, params=params)
    resp.raise_for_status()
    out: dict = {}
    for rec in resp.json():
        icao = rec.get("icaoId")
        info = stations.get(icao)
        temp_c = rec.get("temp")
        obs = rec.get("obsTime")
        if not info or temp_c is None or obs is None:
            continue
        tz_name, target = info
        if datetime.fromtimestamp(int(obs), tz=ZoneInfo(tz_name)).date() != target:
            continue
        temp_f = float(temp_c) * 9.0 / 5.0 + 32.0
        ext = out.setdefault(icao, {"high": temp_f, "low": temp_f})
        ext["high"] = max(ext["high"], temp_f)
        ext["low"] = min(ext["low"], temp_f)
    return out


# ── Open-Meteo fetch ─────────────────────────────────────────────────────────


async def _fetch_forecast(client: httpx.AsyncClient, lat: float, lon: float, target_date: date) -> dict | None:
    """Multi-model super-ensemble daily max AND min temps in one request,
    deterministic fallback. timezone=auto so the daily extremes span the
    station-local calendar day.

    Returns {"high": {members, mean, std, models, source}, "low": {...}}
    (either kind may be absent) or None."""
    date_str = target_date.isoformat()
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "start_date": date_str,
        "end_date": date_str,
        "timezone": "auto",
        "models": ENSEMBLE_MODELS,
    }
    try:
        resp = await client.get(OPEN_METEO_ENSEMBLE_URL, params=params)
        if resp.status_code == 200:
            daily = resp.json().get("daily", {})
            members: dict = {"high": [], "low": []}
            model_counts: dict = {"high": {}, "low": {}}
            for key, values in daily.items():
                m = _MEMBER_KEY_RX.match(key)
                if not m or not values:
                    continue
                vals = [float(v) for v in values if v is not None]
                if not vals:
                    continue
                kind = "high" if m.group("var") == "max" else "low"
                members[kind].extend(vals)
                model = m.group("model") or "default"
                model_counts[kind][model] = model_counts[kind].get(model, 0) + 1
            bundle = {}
            for kind, vals in members.items():
                if vals:
                    bundle[kind] = {
                        "members": vals,
                        "mean": statistics.mean(vals),
                        "std": statistics.stdev(vals) if len(vals) > 1 else 3.0,
                        "models": model_counts[kind],
                        "source": "open-meteo-ensemble",
                    }
            if bundle:
                return bundle
        else:
            log.warning("Open-Meteo ensemble returned %d", resp.status_code)
    except Exception as e:
        log.warning("Open-Meteo ensemble error: %s", e)

    params.pop("models", None)
    try:
        resp = await client.get(OPEN_METEO_URL, params=params)
        if resp.status_code != 200:
            return None
        daily = resp.json().get("daily", {})
        target_dt = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
        hours_ahead = (target_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        std = min(2.5 + 0.03 * max(0, hours_ahead), 6.0)
        bundle = {}
        for kind, var in (("high", "temperature_2m_max"), ("low", "temperature_2m_min")):
            temps = daily.get(var, [])
            if temps and temps[0] is not None:
                mean = float(temps[0])
                bundle[kind] = {"members": [mean], "mean": mean, "std": std, "models": {}, "source": "open-meteo-deterministic"}
        return bundle or None
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


async def compute(markets: list[dict], conn: sqlite3.Connection | None = None) -> list[dict]:
    candidates = []
    for m in markets:
        parsed = parse_weather_market(m.get("question") or "", m.get("venue"), m.get("venue_id"))
        if not parsed:
            continue
        target = parsed["target_date"] or _date_from_iso(m.get("end_date"))
        if not target:
            continue
        local_today = datetime.now(ZoneInfo(parsed["tz"])).date()
        if target < local_today:
            continue
        candidates.append((m, parsed, target, target == local_today))
    if not candidates:
        return []

    ledger = _load_ledger(conn)
    bias_map = ledger["bias"]
    sigma_k = _sigma_from_residual(ledger["residual_std"])

    rows: list[dict] = []
    cache: dict = {}
    stations_used: set = set()
    extremes_map: dict = {}
    same_day = {p["icao"]: (p["tz"], t) for _, p, t, is_today in candidates if is_today}
    async with httpx.AsyncClient(timeout=15.0) as client:
        if same_day:
            try:
                extremes_map = await _fetch_metar_extremes(client, same_day)
            except Exception as e:
                log.warning("METAR fetch failed; intraday constraint skipped: %s", e)
        for m, parsed, target, is_today in candidates:
            key = (parsed["icao"], target.isoformat())
            if key not in cache:
                if parsed["icao"] not in stations_used and len(stations_used) >= _MAX_STATIONS:
                    continue
                stations_used.add(parsed["icao"])
                cache[key] = await _fetch_forecast(client, parsed["lat"], parsed["lon"], target)
            kind = parsed["kind"]
            forecast = (cache[key] or {}).get(kind)
            if not forecast:
                continue

            members = forecast["members"]
            bias = bias_map.get((parsed["icao"], kind), 0.0)
            observed = extremes_map.get(parsed["icao"], {}).get(kind) if is_today else None
            runmax = observed if kind == "high" else None
            runmin = observed if kind == "low" else None
            if len(members) >= _MIN_ENSEMBLE_MEMBERS:
                prob = dressed_probability(members, parsed, sigma_k, bias=bias, runmax=runmax, runmin=runmin)
                method = "ensemble_intraday" if observed is not None else "ensemble_dressed"
            else:
                prob = gaussian_probability(forecast["mean"] + bias, forecast["std"], parsed, runmax=runmax, runmin=runmin)
                method = "gaussian_intraday" if observed is not None else "gaussian"

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
                            "kind": kind,
                            "threshold": parsed["threshold"],
                            "temp_lower": parsed["temp_lower"],
                            "temp_upper": parsed["temp_upper"],
                            "is_over": parsed["is_over"],
                            "members": len(members),
                            "mean": round(forecast["mean"], 2),
                            "std": round(forecast["std"], 2),
                            "forecast_source": forecast["source"],
                            "bias": round(bias, 3),
                            "sigma_k": round(sigma_k, 3),
                            "runmax": round(runmax, 2) if runmax is not None else None,
                            "runmin": round(runmin, 2) if runmin is not None else None,
                            "models": forecast.get("models", {}),
                            "tz": parsed["tz"],
                        }
                    ),
                }
            )
    return rows
