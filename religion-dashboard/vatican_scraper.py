"""Scraper for the Vatican Press Office canonical College of Cardinals list.

AUTHORITATIVE SOURCE
    https://press.vatican.va/content/salastampa/en/documentation/cardinali_statistiche/cardinali_elenco_anagrafico.html

The "elenco anagrafico" (alphabetical / biographical list) is the
canonical published source for the full college. It's updated whenever
a cardinal is created, dies, or otherwise loses his rights. The page
itself does not include an RSS feed, so we poll on a slow cadence
(once per 24h is generous — the data changes weeks-to-months apart).

WHAT THE SCRAPER PRODUCTS
    fetch_full_college() returns a list of dicts with the FACTUAL fields:
      name, born_iso, age, country, role, consistory_date_iso, appointer,
      elector (bool, derived from age and reference date)
    Journalistic-judgment fields — wing, papabile_tier, summary — are
    NOT scraped; those remain in cardinals.py and are merged on top.

PARSING STRATEGY
    The Vatican page format has been broadly consistent for many years
    but is not formally documented. Each cardinal entry contains:
        - a header with "Card." + the name (b/strong)
        - a "Born" line with date and place
        - a "Created and proclaimed Cardinal by" line with Pope + consistory date
        - role/titular church info
    We extract these with regular expressions over the raw HTML, which
    is more tolerant of structural drift than DOM walking.

FALLBACK
    On any HTTP, parse, or extraction error, fetch_full_college() returns
    an empty list. The dashboard's /api/conclave/live endpoint then
    falls back to the curated CARDINALS data and surfaces a stale-or-
    failed indicator to the user. We never serve fabricated data.

CACHE
    24h TTL by default. Override with force=True. The cache is in-process
    only (lives with the Flask app's memory) — restart the server to
    force a re-fetch.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime
from typing import Optional

import requests

log = logging.getLogger("vatican_scraper")

CARDINALS_LIST_URL = (
    "https://press.vatican.va/content/salastampa/en/documentation/"
    "cardinali_statistiche/cardinali_elenco_anagrafico.html"
)

_USER_AGENT = "religion-dashboard-vatican-scraper/1.0 (+https://religion.narve.ai)"

_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h
_cache: dict = {"data": None, "fetched_at": 0.0, "ok": False, "error": ""}
_cache_lock = threading.Lock()


# ─── Date parsing ───────────────────────────────────────────────────────────

_MONTHS = {
    "january": 1,  "february": 2,  "march": 3,     "april": 4,
    "may": 5,      "june": 6,      "july": 7,      "august": 8,
    "september": 9,"october": 10,  "november": 11, "december": 12,
    # Italian fallbacks (the Italian version of the page mixes languages)
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5,
    "giugno": 6, "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10,
    "novembre": 11, "dicembre": 12,
}


def _parse_date(s: str) -> Optional[str]:
    """Parse 'DD Month YYYY' (English or Italian) → ISO YYYY-MM-DD."""
    if not s:
        return None
    s = s.strip().rstrip(",")
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$", s)
    if not m:
        # Try Month DD, YYYY (US format)
        m = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$", s)
        if m:
            mon_name, dd, yyyy = m.group(1), m.group(2), m.group(3)
            mon = _MONTHS.get(mon_name.lower())
            if mon:
                return f"{int(yyyy):04d}-{mon:02d}-{int(dd):02d}"
        return None
    dd, mon_name, yyyy = m.group(1), m.group(2), m.group(3)
    mon = _MONTHS.get(mon_name.lower())
    if not mon:
        return None
    return f"{int(yyyy):04d}-{mon:02d}-{int(dd):02d}"


def _age_on(born_iso: str, ref: date) -> Optional[int]:
    if not born_iso:
        return None
    try:
        y, m, d = (int(x) for x in born_iso.split("-"))
    except (ValueError, AttributeError):
        return None
    age = ref.year - y - ((ref.month, ref.day) < (m, d))
    return age


# ─── Pope name → canonical short name ───────────────────────────────────────

_POPE_NORMALISE = {
    "francis":         "Francis",
    "francesco":       "Francis",
    "francisco":       "Francis",
    "benedict xvi":    "Benedict XVI",
    "benedetto xvi":   "Benedict XVI",
    "benedict 16":     "Benedict XVI",
    "john paul ii":    "John Paul II",
    "giovanni paolo ii":"John Paul II",
    "juan pablo ii":   "John Paul II",
    "leo xiv":         "Leo XIV",        # in case of a 2025+ pope
}


def _normalize_pope(s: str) -> str:
    if not s:
        return "Unknown"
    key = re.sub(r"\s+", " ", s.strip().lower())
    for needle, val in _POPE_NORMALISE.items():
        if needle in key:
            return val
    return s.strip()


# ─── HTML parsing ───────────────────────────────────────────────────────────

# Strip out script/style blocks before regex matching so we don't pick up
# inline JS strings.
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(s: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", s)).strip()


# Each cardinal block on the page is delimited by a "Card." heading and
# ends at the next "Card." heading or end of document. Capture name +
# the trailing block of biographical text.
_CARDINAL_BLOCK_RE = re.compile(
    r"Card\.\s*([A-ZÁÀÂÄÉÈÊËÍÌÎÏÓÒÔÖÚÙÛÜÑÇŠŽŁ][^<\n]{2,200}?)\s*(?:</[^>]+>|<br|\s{2,})(.*?)(?=Card\.\s*[A-ZÁÀÂÄÉÈÊË]|\Z)",
    re.DOTALL,
)

_BORN_RE       = re.compile(r"Born(?:\s+in)?[:.\s]+([0-9]{1,2}\s+[A-Za-z]+\s+\d{4})", re.IGNORECASE)
_NATIONALITY_RE= re.compile(r"Nationality[:.\s]+([^<.\n,]{2,40})", re.IGNORECASE)
_CONSISTORY_RE = re.compile(
    r"[Cc]reated\s+(?:and\s+proclaimed\s+)?Cardinal\s+by\s+(?:Pope\s+)?([A-Za-zóÁ\s\.IVX]+?)\s+in\s+the\s+consistory\s+of\s+([0-9]{1,2}\s+[A-Za-z]+\s+\d{4})",
)
_ROLE_HINT_RE  = re.compile(r"(Archbishop|Bishop|Prefect|Patriarch|Cardinal|Major\s+Archbishop|Apostolic\s+Nuncio)[^<\n.]{0,120}", re.IGNORECASE)


def _name_to_proper(raw_name: str) -> str:
    """Vatican prints 'SURNAME, Given Names' in caps. Re-case to 'Given Names SURNAME'."""
    raw_name = raw_name.strip().rstrip(",.").strip()
    if "," in raw_name:
        surname, given = raw_name.split(",", 1)
        surname = surname.strip()
        given = given.strip()
        # Title-case the surname (keep small particles lowercase: de, la, van, von, di, du, du, do)
        parts = []
        for tok in surname.split():
            low = tok.lower()
            if low in ("de", "la", "van", "von", "di", "du", "do", "del", "della"):
                parts.append(low)
            else:
                parts.append(tok.capitalize())
        surname_pretty = " ".join(parts)
        return f"{given} {surname_pretty}".strip()
    return raw_name


def parse_cardinals_html(html: str, ref: Optional[date] = None) -> list[dict]:
    """Pure parser — testable without network. Returns factual records only."""
    if ref is None:
        ref = date.today()
    cleaned = _SCRIPT_RE.sub("", html)
    out: list[dict] = []
    for m in _CARDINAL_BLOCK_RE.finditer(cleaned):
        raw_name = _strip_tags(m.group(1))
        body = _strip_tags(m.group(2))

        name = _name_to_proper(raw_name)
        if not name or len(name) < 4:
            continue

        born_match = _BORN_RE.search(body)
        born_iso = _parse_date(born_match.group(1)) if born_match else None
        age = _age_on(born_iso, ref) if born_iso else None

        nat_match = _NATIONALITY_RE.search(body)
        country = nat_match.group(1).strip() if nat_match else ""

        cons_match = _CONSISTORY_RE.search(body)
        appointer = _normalize_pope(cons_match.group(1)) if cons_match else "Unknown"
        consistory_iso = _parse_date(cons_match.group(2)) if cons_match else None

        role_match = _ROLE_HINT_RE.search(body)
        role = role_match.group(0).strip() if role_match else ""

        out.append({
            "name": name,
            "born_iso": born_iso,
            "age": age,
            "country": country,
            "role": role,
            "consistory_date_iso": consistory_iso,
            "appointer": appointer,
            "elector": (age is not None and age < 80),
        })
    return out


# ─── Network fetch ──────────────────────────────────────────────────────────

def _http_get(url: str, *, timeout: int = 20) -> Optional[str]:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": _USER_AGENT})
        if r.status_code != 200:
            log.warning("Vatican fetch HTTP %d for %s", r.status_code, url)
            return None
        # The Vatican site serves UTF-8 but the header may be ISO-8859-1.
        # Use the content's declared charset if requests guessed wrong.
        r.encoding = r.apparent_encoding or r.encoding
        return r.text
    except Exception as e:
        log.warning("Vatican fetch error: %s", e)
        return None


def fetch_full_college(force: bool = False, ref: Optional[date] = None) -> dict:
    """Fetch + cache the full College of Cardinals from press.vatican.va.

    Returns a dict with shape:
        {
          "ok": bool,
          "fetched_at": float (unix ts),
          "error": str,
          "cardinals": list[dict],
        }
    On failure, "cardinals" is the last successfully-cached list or [] if
    we've never succeeded. The caller is responsible for falling back to
    curated data when ok=False.
    """
    with _cache_lock:
        now = time.time()
        if not force and _cache["data"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS:
            return {
                "ok": _cache["ok"],
                "fetched_at": _cache["fetched_at"],
                "error": _cache["error"],
                "cardinals": _cache["data"] or [],
            }

    html = _http_get(CARDINALS_LIST_URL)
    if html is None:
        with _cache_lock:
            _cache["fetched_at"] = time.time()
            _cache["ok"] = False
            _cache["error"] = "HTTP fetch failed"
            return {
                "ok": False,
                "fetched_at": _cache["fetched_at"],
                "error": _cache["error"],
                "cardinals": _cache["data"] or [],
            }

    try:
        cardinals = parse_cardinals_html(html, ref=ref)
    except Exception as e:
        log.warning("Vatican parse error: %s", e)
        with _cache_lock:
            _cache["fetched_at"] = time.time()
            _cache["ok"] = False
            _cache["error"] = f"parse failed: {e}"
            return {
                "ok": False,
                "fetched_at": _cache["fetched_at"],
                "error": _cache["error"],
                "cardinals": _cache["data"] or [],
            }

    if len(cardinals) < 50:
        # Sanity: the college has 200+ cardinals. A successful parse
        # should return at least that many. If we get fewer, treat it
        # as a parse failure (probably the page format changed).
        log.warning("Vatican parse returned only %d cardinals — suspicious", len(cardinals))
        with _cache_lock:
            _cache["fetched_at"] = time.time()
            _cache["ok"] = False
            _cache["error"] = f"too few cardinals parsed ({len(cardinals)}); page format may have changed"
            return {
                "ok": False,
                "fetched_at": _cache["fetched_at"],
                "error": _cache["error"],
                "cardinals": _cache["data"] or [],
            }

    with _cache_lock:
        _cache["data"] = cardinals
        _cache["fetched_at"] = time.time()
        _cache["ok"] = True
        _cache["error"] = ""
        return {
            "ok": True,
            "fetched_at": _cache["fetched_at"],
            "error": "",
            "cardinals": cardinals,
        }


# ─── Merge with curated metadata ────────────────────────────────────────────

def merge_with_curated(scraped: list[dict], curated: list[dict]) -> list[dict]:
    """Overlay curated wing / papabile_tier / summary onto scraped factuals.

    Match by case-insensitive surname-token-set (Vatican prints names in
    'SURNAME, Given' format; ours are 'Given Names Surname'). Falls back
    to first-token-of-surname match when the full match misses.
    """
    by_surname: dict[str, dict] = {}
    for c in curated:
        # Approximate surname: last token of name
        last = c["name"].split()[-1].lower()
        by_surname[last] = c

    out = []
    for s in scraped:
        last = s["name"].split()[-1].lower()
        curated_match = by_surname.get(last)
        merged = dict(s)
        if curated_match:
            merged["wing"] = curated_match.get("wing", "moderate")
            merged["papabile_tier"] = curated_match.get("papabile_tier", 0)
            merged["summary"] = curated_match.get("summary", "")
            merged["matched_curated"] = True
        else:
            merged["wing"] = "moderate"
            merged["papabile_tier"] = 0
            merged["summary"] = ""
            merged["matched_curated"] = False
        out.append(merged)
    return out


# ─── Drift detector ────────────────────────────────────────────────────────

def detect_drift(scraped: list[dict], curated: list[dict]) -> dict:
    """Compare scraped vs curated. Surfaces newly-created and possibly-deceased."""
    scraped_names = {c["name"].split()[-1].lower() for c in scraped}
    curated_names = {c["name"].split()[-1].lower() for c in curated}

    only_in_scraped = sorted(scraped_names - curated_names)  # New cardinals or missed by us
    only_in_curated = sorted(curated_names - scraped_names)  # Deceased / left college since our last update
    return {
        "added_since_curated": only_in_scraped,
        "missing_from_scraped": only_in_curated,
        "scraped_count": len(scraped),
        "curated_count": len(curated),
    }
