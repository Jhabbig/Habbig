"""Outbreak feed aggregator.

Pulls the WHO Disease Outbreak News (DON) feed via WHO's OData JSON API. RSS
for DON was deprecated; the JSON endpoint is what powers the public DON page
itself, so it's the most authoritative source.

WHO DON titles follow the strict format "Disease - Country" (e.g.
"Measles - Bangladesh", "Avian Influenza A(H5N1) - Cambodia"), which makes
country extraction trivial. We resolve to ISO3 by matching against the country
catalog with a small alias table for the historical names WHO uses.

CDC HAN and ProMED RSS feeds are deprecated / paywalled as of 2026, so DON is
the only live source in this module. If we add another source later it should
emit the same shape.

Each normalized outbreak record:
    {
        "id":          "2026-DON598",
        "title":       "Measles - Bangladesh",
        "disease":     "Measles",
        "country_iso3":"BGD",
        "country_name":"Bangladesh",
        "published":   "2026-04-23T12:58:07Z",
        "summary":     "<plain-text overview, 280 chars max>",
        "url":         "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON598",
        "source":      "WHO DON",
    }
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from threading import Lock

from .country_codes import normalize as normalize_iso3, INDEX as COUNTRY_INDEX

log = logging.getLogger(__name__)

WHO_DON_API = "https://www.who.int/api/news/diseaseoutbreaknews"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "outbreaks"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 1h TTL — outbreak feeds update sub-daily and we want freshness without
# hammering WHO. Stale-while-error fallback handles outages.
CACHE_TTL_SECONDS = 60 * 60

_lock = Lock()

# WHO uses some country names that don't exact-match our catalog. Map to ISO3.
WHO_COUNTRY_ALIASES: dict[str, str] = {
    "United States of America": "USA",
    "United Kingdom of Great Britain and Northern Ireland": "GBR",
    "Iran (Islamic Republic of)": "IRN",
    "Russian Federation": "RUS",
    "Republic of Korea": "KOR",
    "Democratic People's Republic of Korea": "PRK",
    "Lao People's Democratic Republic": "LAO",
    "Syrian Arab Republic": "SYR",
    "United Republic of Tanzania": "TZA",
    "Bolivia (Plurinational State of)": "BOL",
    "Venezuela (Bolivarian Republic of)": "VEN",
    "Viet Nam": "VNM",
    "Republic of Moldova": "MDA",
    "Türkiye": "TUR",
    "Turkey": "TUR",
    "Czech Republic": "CZE",
    "Côte d’Ivoire": "CIV",
    "Cote d'Ivoire": "CIV",
    "Democratic Republic of the Congo": "COD",
    "Congo": "COG",
    "United Arab Emirates": "ARE",
    "Cabo Verde": "CPV",
    "Cape Verde": "CPV",
    "Brunei Darussalam": "BRN",
    "Eswatini (Kingdom of)": "SWZ",
    "Kingdom of Eswatini": "SWZ",
    "occupied Palestinian territory, including east Jerusalem": "PSE",
    "Federated States of Micronesia": "FSM",
    "Kingdom of Saudi Arabia": "SAU",
    "United Republic of Tanzania": "TZA",
    # French overseas territories — DON occasionally tags these directly.
    "La Réunion": "FRA",
    "Mayotte": "FRA",
    # Long official forms.
    "Plurinational State of Bolivia": "BOL",
    "Bolivarian Republic of Venezuela": "VEN",
    "Republic of Rwanda": "RWA",
    "Republic of South Africa": "ZAF",
    "Islamic Republic of Iran": "IRN",
    "People's Republic of China": "CHN",
    "Hashemite Kingdom of Jordan": "JOR",
    "State of Israel": "ISR",
    "Sultanate of Oman": "OMN",
    "State of Qatar": "QAT",
    "State of Kuwait": "KWT",
}

# Strings that mean "no single country" (regional, global, multi-country).
NON_COUNTRY_PATTERNS: tuple[str, ...] = (
    "global situation",
    "global update",
    "global",
    "world",
    "multi-country",
    "multi country",
    "multiple countries",
    "region of the americas",
    "region of the european",
    "region of the",
    "european region",
    "african region",
    "americas region",
    "south-east asia region",
    "western pacific region",
    "eastern mediterranean region",
    "afro",
    "amro",
    "emro",
    "euro",
    "searo",
    "wpro",
)

# Reverse: catalog name → ISO3, lowercased for matching.
NAME_TO_ISO: dict[str, str] = {n.lower(): iso for iso, (n, _) in COUNTRY_INDEX.items()}
for full, iso in WHO_COUNTRY_ALIASES.items():
    NAME_TO_ISO[full.lower()] = iso


def _resolve_country(name: str) -> tuple[str | None, str | None, str]:
    """Return (iso3, canonical_name, scope) where scope is one of
    'country', 'multi', 'regional', 'global', 'unknown'.

    Multi-country (e.g. 'Mauritania and Senegal') resolves to the first
    country with scope='multi' so the pin can still anchor somewhere on the
    globe while the UI badges it as multi-country.
    """
    if not name:
        return None, None, "unknown"
    name = name.strip().rstrip(",.;:")
    low = name.lower()

    # Non-country tags first.
    for pat in NON_COUNTRY_PATTERNS:
        if pat in low:
            if "global" in low or "multi" in low or "world" in low:
                return None, name, "global" if "global" in low or "world" in low else "multi"
            return None, name, "regional"

    # Direct match.
    iso = NAME_TO_ISO.get(low)
    if iso:
        return iso, COUNTRY_INDEX[iso][0], "country"
    # Strip parentheticals.
    stripped = re.sub(r"\s*\(.*?\)\s*", "", name).strip()
    if stripped and stripped.lower() in NAME_TO_ISO:
        iso = NAME_TO_ISO[stripped.lower()]
        return iso, COUNTRY_INDEX[iso][0], "country"
    # Multi-country split: 'Mauritania and Senegal', 'A, B and C', 'A, B, C'
    parts = re.split(r"\s+and\s+|,\s*", name)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        for p in parts:
            if p.lower() in NAME_TO_ISO:
                iso = NAME_TO_ISO[p.lower()]
                return iso, COUNTRY_INDEX[iso][0], "multi"
    # Trailing-token fallback: 'Northern China' / 'Western Kenya' / etc.
    tokens = name.split()
    if len(tokens) >= 2:
        last2 = " ".join(tokens[-2:]).lower()
        last1 = tokens[-1].lower()
        for cand in (last2, last1):
            if cand in NAME_TO_ISO:
                iso = NAME_TO_ISO[cand]
                return iso, COUNTRY_INDEX[iso][0], "country"
    return None, name, "unknown"


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s: str, max_len: int = 280) -> str:
    if not s:
        return ""
    txt = html.unescape(_TAG_RE.sub(" ", s))
    txt = _WS_RE.sub(" ", txt).strip()
    if len(txt) > max_len:
        txt = txt[: max_len - 1].rstrip() + "…"
    return txt


_SPLIT_RE = re.compile(r"\s*[-–—]+\s*")

# Compound names containing literal hyphens that we must NOT split on. Replace
# the hyphen with a sentinel before splitting, then restore.
_SENTINEL = "\x00HYPH\x00"
_PROTECTED_HYPHENS: tuple[str, ...] = (
    "Timor-Leste",
    "Guinea-Bissau",
    "Bissau-Guinea",
    "Multi-country",
    "Multi-state",
    "Multi-Country",
    "MERS-CoV",
    "SARS-CoV",
    "SARS-CoV-2",
    "cVDPV1",
    "cVDPV2",
    "cVDPV3",
    # Special characters in disease names
    "non-typhoidal",
)


def _split_title(title: str) -> tuple[str, str]:
    """'Measles - Bangladesh' → ('Measles', 'Bangladesh').

    DONs use -, –, — with varying spacing. Split on the LAST dash-like
    separator after first protecting compound names that contain a hyphen
    (Timor-Leste, MERS-CoV, Multi-country, etc.) from being broken mid-token.
    """
    protected = title
    for tok in _PROTECTED_HYPHENS:
        if tok in protected:
            protected = protected.replace(tok, tok.replace("-", _SENTINEL))
    matches = list(_SPLIT_RE.finditer(protected))
    if not matches:
        return title.strip(), ""
    last = matches[-1]
    head = protected[: last.start()].strip().replace(_SENTINEL, "-")
    tail = protected[last.end() :].strip().replace(_SENTINEL, "-")
    if tail.lower().startswith("the "):
        tail = tail[4:].strip()
    return head, tail


def _normalize_disease(d: str) -> str:
    """Collapse stylistic variants ('A(H5N1)' vs 'A (H5N1)' etc.)."""
    if not d:
        return d
    # Collapse runs of whitespace.
    d = _WS_RE.sub(" ", d).strip()
    # 'Avian Influenza A (H5N1)' -> 'Avian Influenza A(H5N1)'
    d = re.sub(r"A \(([HN0-9]+)\)", r"A(\1)", d)
    return d


def _normalize_don(item: dict) -> dict:
    title = item.get("Title") or ""
    disease, country_raw = _split_title(title)
    disease = _normalize_disease(disease)
    iso, country_name, scope = _resolve_country(country_raw)
    # No country part at all — DON has no per-country focus, treat as global.
    if not country_raw and scope == "unknown":
        scope = "global"
    don_id = item.get("DonId") or item.get("UrlName") or item.get("Id")
    rel = item.get("ItemDefaultUrl") or ""
    if rel and not rel.startswith("http"):
        rel = "https://www.who.int/emergencies/disease-outbreak-news/item" + rel
    summary = _strip_html(item.get("Overview") or item.get("Summary") or "")
    return {
        "id": don_id,
        "title": title,
        "disease": disease,
        "country_iso3": iso,
        "country_name": country_name or country_raw,
        "scope": scope,
        "published": item.get("PublicationDateAndTime") or item.get("PublicationDate"),
        "summary": summary,
        "url": rel,
        "source": "WHO DON",
    }


def _cache_path() -> Path:
    return CACHE_DIR / "who_don.json"


def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Outbreak cache unreadable: %s", exc)
        return None
    if (time.time() - body.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
        return None
    return body


def _write_cache(payload: dict) -> None:
    try:
        _cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.warning("Outbreak cache write failed: %s", exc)


def _fetch_who_don(top: int = 200, timeout: float = 30.0) -> list[dict]:
    """Fetch the most recent `top` DONs in publication-date order.

    WHO's OData endpoint caps $top at 100, so we page using $skip until we
    reach `top` items or run out. Spaces in $orderby must be %20-encoded
    (the endpoint rejects the '+' that urlencode emits by default).
    """
    out: list[dict] = []
    page = 100
    skip = 0
    orderby = urllib.parse.quote("PublicationDateAndTime desc", safe="")
    while len(out) < top:
        size = min(page, top - len(out))
        qs = f"%24top={size}&%24skip={skip}&%24orderby={orderby}"
        url = f"{WHO_DON_API}?{qs}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "world-health-dashboard/0.2",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
            body = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(body)
        batch = parsed.get("value", []) or []
        if not batch:
            break
        out.extend(batch)
        skip += len(batch)
        if len(batch) < size:
            break
    return out


def fetch_outbreaks(force: bool = False, top: int = 200) -> dict:
    """Return the latest outbreak feed:
    {
      "items":      [{...}, ...],
      "fetched_at": <epoch>,
      "stale":      bool,
    }
    """
    with _lock:
        if not force:
            cached = _read_cache()
            if cached:
                return cached

    try:
        raw = _fetch_who_don(top=top)
    except Exception as exc:
        log.warning("WHO DON fetch failed: %s", exc)
        # Stale-while-error: return last-known data.
        p = _cache_path()
        if p.exists():
            try:
                stale = json.loads(p.read_text(encoding="utf-8"))
                stale["stale"] = True
                stale["error"] = str(exc)
                return stale
            except Exception as cache_exc:
                log.warning("outbreak_feeds stale cache read failed (%s); returning empty items", cache_exc)
        return {"items": [], "fetched_at": time.time(), "stale": False, "error": str(exc)}

    items = [_normalize_don(it) for it in raw]
    payload = {
        "items": items,
        "fetched_at": time.time(),
        "stale": False,
    }
    with _lock:
        _write_cache(payload)
    log.info("WHO DON: %d outbreaks fetched", len(items))
    return payload


# ─── Aggregations used by the frontend ─────────────────────────────────────


def by_country(payload: dict | None = None) -> dict[str, list[dict]]:
    """{iso3: [outbreak, ...]} — only items we successfully geocoded."""
    payload = payload or fetch_outbreaks()
    out: dict[str, list[dict]] = {}
    for it in payload.get("items", []):
        iso = it.get("country_iso3")
        if not iso:
            continue
        out.setdefault(iso, []).append(it)
    return out


def by_disease(payload: dict | None = None) -> dict[str, list[dict]]:
    payload = payload or fetch_outbreaks()
    out: dict[str, list[dict]] = {}
    for it in payload.get("items", []):
        d = (it.get("disease") or "Unknown").strip() or "Unknown"
        out.setdefault(d, []).append(it)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = fetch_outbreaks(force=True)
    print(f"items: {len(p['items'])}")
    bd = by_disease(p)
    for disease, items in sorted(bd.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"  {disease:40s} {len(items)}")
    print(f"Most recent: {p['items'][0] if p['items'] else None}")
