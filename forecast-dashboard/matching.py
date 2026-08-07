"""Cross-venue market matching (Polymarket <-> Kalshi).

Three stages inside match(conn), precision over recall — a wrong cross-venue
link on a probability board is worse than a missed one:

1. rulepack:fed — classify rate-decision questions on both venues into a
   shared (decision_month, bucket) vocabulary (cut25 / hold / hike50, ...);
   equal keys link with score 1.0, status 'auto'. Bucket vocab vendored from
   centralbank-dashboard/ingestion/outcome_classifier.py; the per-bucket
   cross-venue join pattern from centralbank-dashboard/analysis/edge.py.
2. rulepack:weather — parse temperature questions on both venues to
   (station_icao, target_date, direction, threshold_bucket); equal tuples
   link 'auto'. Title parsers vendored from
   polymarket_weather_bot/gamma_client.py. Kalshi KXHIGH* tickers supply only
   station and date; the bracket itself comes from the question text, because
   the ticker's B/T suffix does not encode the event (live catalog: B95.5 is
   the "95° to 96°" range bracket and T covers both tails — ">96°" and "<89°"
   are both T). The vendored bare "N°F" fallback is deliberately dropped:
   without an explicit direction it cannot key a market safely.
3. fuzzy — blocked on end-date within +-1 day; a pair is a candidate only if
   every extracted number+unit and every extracted date agrees on both titles
   AND token Jaccard >= 0.45; combined score (mean of Jaccard and difflib
   ratio) >= 0.65 writes status 'suggested' — NEVER 'auto'.

'rejected' rows in venue_links permanently suppress a pair in every stage;
pairs already 'confirmed'/'auto' are left alone, and markets on either side
of such a pair are excluded from new fuzzy suggestions.

disagreements(conn) covers links with status in ('auto', 'confirmed') and
flags pairs whose latest baselines differ by more than
max(0.03, (spread_poly + spread_kalshi) / 2):
    {poly_uid, kalshi_uid, gap, delta, poly_baseline, kalshi_baseline,
     method, status, score, question, urls: {polymarket, kalshi}}
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

import db

# ── rulepack: fed (vendored from centralbank-dashboard) ──────────────────────

_CUT_VERB = (
    r"(?:cut|cuts|cutting|decrease|decreases|decreased|"
    r"reduce|reduces|reduced|lower|lowers|lowered)"
)
_HIKE_VERB = (
    r"(?:hike|hikes|hiked|hiking|increase|increases|increased|"
    r"raise|raises|raised|raising)"
)
_VERB = f"(?:{_CUT_VERB}|{_HIKE_VERB})"
_BPS = r"(?:bps|bp|basis\s*points?)"
# Range markets (">25bps", "at least 50 bps") aggregate several single-step
# buckets — they never map onto one bucket, so they never key a link.
_INEQ = r"(?:>|>=|≥|more\s+than|at\s+least)"
_VERB_BPS_RX = re.compile(rf"({_VERB})\b[^.\n]{{0,40}}?({_INEQ}\s*)?(\d{{1,3}})\s*{_BPS}", re.I)
_BPS_VERB_RX = re.compile(rf"({_INEQ}\s*)?(\d{{1,3}})\s*{_BPS}[^.\n]{{0,30}}?({_VERB})", re.I)
_HOLD_RX = re.compile(
    r"\b(hold|no change|unchanged|leave (?:rates? )?(?:the same|alone)|fed maintains rate)\b",
    re.I,
)
_CUT_RX = re.compile(_CUT_VERB, re.I)

_FED_RX = re.compile(r"\b(fed|fomc|federal reserve|fed(?:eral)? funds)\b", re.I)
_OTHER_CB_RX = re.compile(
    r"\b(ecb|boe|boj|snb|rba|rbnz|pboc|european central bank|bank of england|"
    r"bank of japan|bank of canada|riksbank|norges bank|banxico)\b",
    re.I,
)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_NAMES = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
_MONTH_YEAR_RX = re.compile(rf"\b({_MONTH_NAMES})\.?\s+(\d{{4}})\b", re.I)
_MD_RX = re.compile(rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?\b", re.I)
_ISO_RX = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_SLASH_RX = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def _fed_bucket(text: str) -> str | None:
    q = text or ""
    if _HOLD_RX.search(q):
        return "hold"
    m = _VERB_BPS_RX.search(q)
    if m:
        verb, ineq, bps = m.group(1), m.group(2), int(m.group(3))
        if ineq:
            return None
        if bps == 0:
            return "hold"
        return f"{'cut' if _CUT_RX.fullmatch(verb) else 'hike'}{bps}"
    m = _BPS_VERB_RX.search(q)
    if m:
        ineq, bps, verb = m.group(1), int(m.group(2)), m.group(3)
        if ineq:
            return None
        if bps == 0:
            return "hold"
        return f"{'cut' if _CUT_RX.fullmatch(verb) else 'hike'}{bps}"
    return None


def _infer_year(month: int, day: int, year: int | None = None) -> date | None:
    now = datetime.now(timezone.utc)
    if year is not None:
        y = year if year >= 100 else 2000 + year
    else:
        y = now.year
        try:
            d = date(y, month, day)
        except ValueError:
            return None
        # No explicit year: a date more than a month in the past means the
        # market is about next year's occurrence.
        if (now.date() - d).days > 30:
            y += 1
    try:
        return date(y, month, day)
    except ValueError:
        return None


def _decision_month(question: str, end_date: str | None) -> str | None:
    low = (question or "").lower()
    m = _MONTH_YEAR_RX.search(low)
    if m:
        return f"{int(m.group(2)):04d}-{_MONTHS[m.group(1)[:3]]:02d}"
    m = _MD_RX.search(low)
    if m:
        d = _infer_year(_MONTHS[m.group(1)[:3]], int(m.group(2)), int(m.group(3)) if m.group(3) else None)
        if d:
            return d.strftime("%Y-%m")
    if end_date and len(str(end_date)) >= 7:
        return str(end_date)[:7]
    return None


def _fed_key(m: dict) -> tuple | None:
    q = m.get("question") or ""
    if not _FED_RX.search(q) or _OTHER_CB_RX.search(q):
        return None
    bucket = _fed_bucket(q)
    if not bucket:
        return None
    month = _decision_month(q, m.get("end_date"))
    if not month:
        return None
    return ("fed", month, bucket)


# ── rulepack: weather (vendored from polymarket_weather_bot) ─────────────────

_STATIONS = {
    "new york": "KLGA",
    "nyc": "KLGA",
    "chicago": "KORD",
    "dallas": "KDAL",
    "miami": "KMIA",
    "los angeles": "KLAX",
    "la": "KLAX",
    "denver": "KDEN",
    "london": "EGLC",
    "paris": "LFPO",
    "tokyo": "RJTT",
    "seoul": "RKSS",
    "sydney": "YSSY",
}
_CITY_ALIASES = {
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
_CITY_RXS = [(re.compile(r"\b" + re.escape(c) + r"\b"), c) for c in sorted(set(_STATIONS) | set(_CITY_ALIASES), key=len, reverse=True)]

_KALSHI_SERIES_CITY = {
    "KXHIGHNY": "new york",
    "KXHIGHCHI": "chicago",
    "KXHIGHMIA": "miami",
    "KXHIGHLAX": "los angeles",
    "KXHIGHDEN": "denver",
}
_KALSHI_TICKER_RX = re.compile(r"^([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-[BT][\d.]+$")

_TEMP_CUE_RX = re.compile(r"°|\btemperatures?\b|\btemp\b|\bdegrees\b")
_DEC = r"(\d+(?:\.\d+)?)"
_OVER_RXS = [
    re.compile(p)
    for p in (
        rf"{_DEC}\s*°?\s*f?\s*or\s*(?:higher|more|above)",
        rf"(?:above|over|exceed|at\s+least)\s*{_DEC}\s*°?\s*f?",
        rf"{_DEC}\s*°?\s*f?\s*\+",
        rf"≥\s*{_DEC}",
    )
]
_UNDER_RXS = [
    re.compile(p)
    for p in (
        rf"{_DEC}\s*°?\s*f?\s*or\s*(?:lower|less|below)",
        rf"(?:below|under)\s*{_DEC}\s*°?\s*f?",
        rf"≤\s*{_DEC}",
    )
]
_RANGE_RXS = [
    re.compile(p)
    for p in (
        rf"{_DEC}\s*[-–]\s*{_DEC}\s*°\s*f?",
        rf"between\s*{_DEC}\s*(?:°\s*f?)?\s*and\s*{_DEC}\s*°?\s*f?",
        rf"{_DEC}\s*°\s*f?\s*to\s*{_DEC}\s*°?\s*f?",
    )
]


def _temp_bucket(threshold: float, is_over: bool) -> tuple:
    # Kalshi bracket boundaries sit on half degrees ("above 45.5") while
    # Polymarket phrases integers ("46° or higher") — ceil/floor lands both
    # on the same resolving integer temperature.
    if is_over:
        return ("over", math.ceil(threshold))
    return ("under", math.floor(threshold))


def _temp_part(low: str) -> tuple | None:
    for rx in _OVER_RXS:
        m = rx.search(low)
        if m:
            return _temp_bucket(float(m.group(1)), True)
    for rx in _UNDER_RXS:
        m = rx.search(low)
        if m:
            return _temp_bucket(float(m.group(1)), False)
    for rx in _RANGE_RXS:
        m = rx.search(low)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            if lo >= hi:
                return None
            return ("range", (math.floor(lo), math.floor(hi)))
    return None


def _city_from_title(low: str) -> str | None:
    for rx, c in _CITY_RXS:
        if rx.search(low):
            return _CITY_ALIASES.get(c, c)
    return None


def _date_from_title(low: str) -> date | None:
    m = _MD_RX.search(low)
    if m:
        return _infer_year(_MONTHS[m.group(1)[:3]], int(m.group(2)), int(m.group(3)) if m.group(3) else None)
    m = _ISO_RX.search(low)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _SLASH_RX.search(low)
    if m:
        mth, dd = int(m.group(1)), int(m.group(2))
        if not 1 <= mth <= 12:
            return None
        return _infer_year(mth, dd, int(m.group(3)) if m.group(3) else None)
    return None


def _weather_key_from_title(question: str) -> tuple | None:
    low = (question or "").lower()
    if not low or not _TEMP_CUE_RX.search(low):
        return None
    city = _city_from_title(low)
    if not city:
        return None
    d = _date_from_title(low)
    if d is None:
        return None
    part = _temp_part(low)
    if part is None:
        return None
    return ("weather", _STATIONS[city], d.isoformat()) + part


def _weather_key_from_ticker(m: dict) -> tuple | None:
    for tick in (m.get("venue_id") or "", m.get("event_ticker") or ""):
        mt = _KALSHI_TICKER_RX.match(str(tick).upper())
        if not mt:
            continue
        city = _KALSHI_SERIES_CITY.get(mt.group(1))
        month = _MONTHS.get(mt.group(3).lower())
        if not city or not month:
            continue
        try:
            d = date(2000 + int(mt.group(2)), month, int(mt.group(4)))
        except ValueError:
            continue
        part = _temp_part((m.get("question") or "").lower())
        if part is None:
            return None
        return ("weather", _STATIONS[city], d.isoformat()) + part
    return None


def _weather_key(m: dict) -> tuple | None:
    if m.get("venue") == "kalshi":
        k = _weather_key_from_ticker(m)
        if k:
            return k
    return _weather_key_from_title(m.get("question") or "")


# ── fuzzy suggester ──────────────────────────────────────────────────────────

_JACCARD_MIN = 0.45
_SCORE_MIN = 0.65
_STOPWORDS = {
    "will",
    "the",
    "a",
    "an",
    "be",
    "in",
    "on",
    "at",
    "of",
    "to",
    "by",
    "for",
    "or",
    "and",
    "is",
    "are",
    "it",
    "its",
    "their",
    "this",
    "that",
    "with",
    "as",
    "than",
    "do",
    "does",
    "did",
    "was",
    "were",
    "from",
    "into",
}
_TOKEN_RX = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_NUM_RX = re.compile(
    r"(\$\s*)?(?<![\w.])(\d+(?:,\d{3})*(?:\.\d+)?)(?![\d,])\s*"
    r"(%|°\s*[cf]?|bps\b|bp\b|basis\s*points?|k\b|m\b|bn\b|billion\b|million\b|thousand\b)?",
    re.I,
)


def _blank_spans(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            chars[i] = " "
    return "".join(chars)


def _dates_and_stripped(low: str) -> tuple[set[str], str]:
    """All date mentions normalized ('YYYY-MM-DD', or 'YYYY-MM' for month-year),
    plus the text with those spans blanked so their digits don't leak into
    number extraction."""
    dates: set[str] = set()
    spans: list[tuple[int, int]] = []
    for m in _ISO_RX.finditer(low):
        try:
            dates.add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat())
        except ValueError:
            continue
        spans.append(m.span())
    for m in _MD_RX.finditer(low):
        d = _infer_year(_MONTHS[m.group(1)[:3]], int(m.group(2)), int(m.group(3)) if m.group(3) else None)
        if d is None:
            continue
        dates.add(d.isoformat())
        spans.append(m.span())
    for m in _MONTH_YEAR_RX.finditer(low):
        dates.add(f"{int(m.group(2)):04d}-{_MONTHS[m.group(1)[:3]]:02d}")
        spans.append(m.span())
    for m in _SLASH_RX.finditer(low):
        mth, dd = int(m.group(1)), int(m.group(2))
        if not 1 <= mth <= 12:
            continue
        d = _infer_year(mth, dd, int(m.group(3)) if m.group(3) else None)
        if d is None:
            continue
        dates.add(d.isoformat())
        spans.append(m.span())
    return dates, _blank_spans(low, spans)


def _numbers(text: str) -> set[tuple[float, str]]:
    out: set[tuple[float, str]] = set()
    for m in _NUM_RX.finditer(text):
        val = float(m.group(2).replace(",", ""))
        unit = (m.group(3) or "").lower().replace(" ", "")
        if unit in ("k", "thousand"):
            val, unit = val * 1e3, ""
        elif unit in ("m", "million"):
            val, unit = val * 1e6, ""
        elif unit in ("bn", "billion"):
            val, unit = val * 1e9, ""
        elif unit.startswith("°"):
            unit = "°"
        elif unit in ("bp", "bps") or unit.startswith("basis"):
            unit = "bps"
        if m.group(1):
            unit = "$" + unit
        out.add((val, unit))
    return out


def _norm_tokens(low: str) -> list[str]:
    return [t for t in (tok.strip(".") for tok in _TOKEN_RX.findall(low)) if t and t not in _STOPWORDS]


def _parse_iso_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _features(m: dict) -> dict | None:
    end = _parse_iso_dt(m.get("end_date"))
    q = (m.get("question") or "").strip()
    if end is None or not q:
        return None
    low = q.lower()
    dates, stripped = _dates_and_stripped(low)
    tokens = _norm_tokens(low)
    if not tokens:
        return None
    return {
        "end": end,
        "dates": dates,
        "nums": _numbers(stripped),
        "tokset": set(tokens),
        "text": " ".join(tokens),
    }


def _fuzzy_score(a: dict, b: dict) -> float | None:
    if abs(a["end"] - b["end"]) > timedelta(days=1):
        return None
    if a["nums"] != b["nums"] or a["dates"] != b["dates"]:
        return None
    union = a["tokset"] | b["tokset"]
    if not union:
        return None
    jaccard = len(a["tokset"] & b["tokset"]) / len(union)
    if jaccard < _JACCARD_MIN:
        return None
    score = 0.5 * jaccard + 0.5 * SequenceMatcher(None, a["text"], b["text"]).ratio()
    return score if score >= _SCORE_MIN else None


# ── public API ───────────────────────────────────────────────────────────────


def match(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT uid, venue, venue_id, event_ticker, question, end_date FROM markets WHERE active = 1 AND resolved = 0").fetchall()
    poly = [dict(r) for r in rows if r["venue"] == "polymarket"]
    kalshi = [dict(r) for r in rows if r["venue"] == "kalshi"]
    if not poly or not kalshi:
        return None

    existing = {(link["poly_uid"], link["kalshi_uid"]): link["status"] for link in db.links(conn)}
    rejected = {pair for pair, st in existing.items() if st == "rejected"}
    settled = {pair for pair, st in existing.items() if st in ("auto", "confirmed")}

    for method, keyer in (("rulepack:fed", _fed_key), ("rulepack:weather", _weather_key)):
        kal_by_key: dict[tuple, list[dict]] = {}
        for m in kalshi:
            k = keyer(m)
            if k:
                kal_by_key.setdefault(k, []).append(m)
        if not kal_by_key:
            continue
        for pm in poly:
            k = keyer(pm)
            if not k:
                continue
            for km in kal_by_key.get(k, []):
                pair = (pm["uid"], km["uid"])
                if pair in rejected:
                    continue
                db.upsert_link(conn, pm["uid"], km["uid"], method, 1.0, "auto")
                settled.add(pair)

    settled_poly = {p for p, _ in settled}
    settled_kalshi = {k for _, k in settled}

    kal_feats = [(f, m) for m in kalshi if m["uid"] not in settled_kalshi if (f := _features(m))]
    for pm in poly:
        if pm["uid"] in settled_poly:
            continue
        pf = _features(pm)
        if pf is None:
            continue
        best: tuple[float, dict] | None = None
        for kf, km in kal_feats:
            if (pm["uid"], km["uid"]) in rejected:
                continue
            score = _fuzzy_score(pf, kf)
            if score is None:
                continue
            if best is None or score > best[0]:
                best = (score, km)
        if best:
            db.upsert_link(conn, pm["uid"], best[1]["uid"], "fuzzy", round(best[0], 4), "suggested")
    return None


def disagreements(conn: sqlite3.Connection) -> list[dict]:
    out: list[dict] = []
    for link in db.links(conn):
        if link["status"] not in ("auto", "confirmed"):
            continue
        p = db.latest_snapshot(conn, link["poly_uid"])
        k = db.latest_snapshot(conn, link["kalshi_uid"])
        if not p or not k:
            continue
        pb, kb = p.get("baseline_prob"), k.get("baseline_prob")
        if pb is None or kb is None:
            continue
        gap = abs(pb - kb)
        if gap <= max(0.03, ((p.get("spread") or 0.0) + (k.get("spread") or 0.0)) / 2):
            continue
        pm = db.get_market(conn, link["poly_uid"]) or {}
        km = db.get_market(conn, link["kalshi_uid"]) or {}
        out.append(
            {
                "poly_uid": link["poly_uid"],
                "kalshi_uid": link["kalshi_uid"],
                "gap": round(gap, 4),
                "delta": round(pb - kb, 4),
                "poly_baseline": pb,
                "kalshi_baseline": kb,
                "method": link["method"],
                "status": link["status"],
                "score": link["score"],
                "question": pm.get("question") or km.get("question"),
                "urls": {"polymarket": pm.get("url"), "kalshi": km.get("url")},
            }
        )
    out.sort(key=lambda r: r["gap"], reverse=True)
    return out
