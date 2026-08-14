"""ACLED religious-violence feed.

ACLED (Armed Conflict Location & Event Data) is the gold-standard
event-level conflict dataset. Free tier requires registration at
acleddata.com → an API key + the registered email. We filter their
event stream for religious-violence indicators and surface a country-
level rollup that cross-references our USCIRF designations.

CONFIG (env vars, both required for live fetch):
    ACLED_EMAIL   — the email used at ACLED registration
    ACLED_API_KEY — generated in the ACLED dashboard

Without these, fetch_recent_violence() returns ok=False and the
endpoint serves an empty list with a clear error indicator.

CACHE: 6h TTL. ACLED publishes weekly; refreshing every 6h is plenty.

WHAT WE FILTER FOR:
    - sub_event_type in {Mob violence, Attack, Violence against civilians}
    - event notes containing religious-violence keywords (church, mosque,
      synagogue, temple, gurdwara, monastery, cleric, imam, priest,
      pastor, religious, sectarian, blasphemy, apostasy, jihad, crusade)
    - actor1 or actor2 names containing recognised religion-motivated
      groups (Boko Haram, ISIS, ISWAP, Al-Shabaab, Houthi, Jamaat
      Nasr al-Islam, BJP-affiliated, RSS, Hindutva, Tablighi Jamaat,
      Buddhist Power Force, Bodu Bala Sena, etc.)
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import date, timedelta
from typing import Optional

import requests

log = logging.getLogger("acled_client")

ACLED_BASE = "https://api.acleddata.com/acled/read"

_USER_AGENT = "religion-dashboard-acled-client/1.0 (+https://religion.narve.ai)"

_CACHE_TTL = 6 * 60 * 60   # 6h
_cache: dict = {"data": None, "fetched_at": 0.0, "ok": False, "error": ""}
_cache_lock = threading.Lock()


# ─── Filters ────────────────────────────────────────────────────────────────

RELIGIOUS_KEYWORDS = [
    # Places of worship
    "church", "mosque", "synagogue", "temple", "gurdwara", "monastery",
    "cathedral", "shrine", "pagoda", "chapel",
    # Religious officials
    "imam", "priest", "pastor", "rabbi", "bhikkhu", "monk", "nun",
    "cleric", "ulema", "ayatollah", "muezzin", "bishop", "cardinal",
    # Religion-violence specific
    "religious", "sectarian", "blasphemy", "apostasy",
    "jihad", "crusade", "infidel", "kafir",
    # Communal indicators
    "communal violence", "interfaith", "religious tension",
    "religious minority", "religious persecution",
]

RELIGION_ACTORS = [
    "Boko Haram", "ISWAP", "Islamic State", "ISIS", "ISIL",
    "Al-Shabaab", "Al Shabaab", "Al-Qaeda", "Al Qaeda",
    "Houthi", "Ansar Allah", "Hezbollah",
    "Hayat Tahrir al-Sham", "HTS", "Jamaat Nasr al-Islam", "JNIM",
    "Taliban", "TTP", "Lashkar-e-Taiba",
    "Bodu Bala Sena", "Buddhist Power Force",
    "RSS militants", "Bajrang Dal", "Hindu Sena",
    "Settler", "Hilltop Youth",
]


_KEY_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in RELIGIOUS_KEYWORDS) + r")\b",
    re.IGNORECASE,
)
_ACTOR_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in RELIGION_ACTORS) + r")\b",
    re.IGNORECASE,
)


def is_religious_violence(event: dict) -> tuple[bool, str]:
    """Return (matches, reason) for an ACLED event dict."""
    notes = (event.get("notes") or "")
    actor1 = (event.get("actor1") or "")
    actor2 = (event.get("actor2") or "")
    blob = f"{notes} {actor1} {actor2}"

    am = _ACTOR_RE.search(blob)
    if am:
        return True, f"actor: {am.group(1)}"
    km = _KEY_RE.search(notes)
    if km:
        return True, f"keyword: {km.group(1)}"
    return False, ""


# ─── HTTP fetch ─────────────────────────────────────────────────────────────

def _http_get_acled(params: dict, *, timeout: int = 25) -> Optional[list[dict]]:
    """Run an ACLED API query. Returns list[event] or None on error."""
    email = os.environ.get("ACLED_EMAIL", "").strip()
    api_key = os.environ.get("ACLED_API_KEY", "").strip()
    if not email or not api_key:
        log.warning("ACLED creds not set (need ACLED_EMAIL + ACLED_API_KEY)")
        return None
    payload = {**params, "email": email, "key": api_key, "format": "json", "limit": 5000}
    try:
        r = requests.get(ACLED_BASE, params=payload, timeout=timeout,
                         headers={"User-Agent": _USER_AGENT})
    except Exception as e:
        log.warning("ACLED HTTP error: %s", e)
        return None
    if r.status_code != 200:
        log.warning("ACLED HTTP %d", r.status_code)
        return None
    try:
        payload = r.json()
    except Exception as e:
        log.warning("ACLED JSON parse: %s", e)
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("data") or []


def fetch_recent_violence(days_back: int = 30, force: bool = False) -> dict:
    """Fetch + cache + filter the last N days of ACLED religious-violence events.

    Returns {ok, fetched_at, error, days_back, events, by_country, total_events}.
    On failure (no creds, HTTP, parse), serves the cached value or an empty
    list with an honest error message.
    """
    with _cache_lock:
        now = time.time()
        if not force and _cache["data"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL:
            return _cache["data"]

    end_date = date.today()
    start_date = end_date - timedelta(days=days_back)
    params = {
        "event_date": f"{start_date.isoformat()}|{end_date.isoformat()}",
        "event_date_where": "BETWEEN",
        "fields": "event_id_cnty|event_date|event_type|sub_event_type|actor1|actor2|country|admin1|location|fatalities|notes|source",
    }

    raw = _http_get_acled(params)
    if raw is None:
        result = {
            "ok": False,
            "fetched_at": time.time(),
            "error": "ACLED fetch failed (creds missing or API error)",
            "days_back": days_back,
            "events": [],
            "by_country": {},
            "total_events": 0,
        }
        with _cache_lock:
            _cache["data"] = result
            _cache["fetched_at"] = result["fetched_at"]
        return result

    # Filter and normalise
    filtered: list[dict] = []
    for e in raw:
        ok, reason = is_religious_violence(e)
        if not ok:
            continue
        filtered.append({
            "event_id": e.get("event_id_cnty"),
            "date": e.get("event_date"),
            "event_type": e.get("event_type"),
            "sub_event_type": e.get("sub_event_type"),
            "actor1": e.get("actor1"),
            "actor2": e.get("actor2"),
            "country": e.get("country"),
            "admin1": e.get("admin1"),
            "location": e.get("location"),
            "fatalities": int(e.get("fatalities") or 0),
            "notes": (e.get("notes") or "")[:400],
            "source": e.get("source"),
            "match_reason": reason,
        })

    # Newest first
    filtered.sort(key=lambda x: x["date"] or "", reverse=True)

    # Country rollup
    by_country: dict[str, dict] = {}
    for e in filtered:
        c = e["country"] or "Unknown"
        bucket = by_country.setdefault(c, {"events": 0, "fatalities": 0})
        bucket["events"] += 1
        bucket["fatalities"] += e["fatalities"]

    result = {
        "ok": True,
        "fetched_at": time.time(),
        "error": "",
        "days_back": days_back,
        "events": filtered,
        "by_country": by_country,
        "total_events": len(filtered),
    }
    with _cache_lock:
        _cache["data"] = result
        _cache["fetched_at"] = result["fetched_at"]
    return result
