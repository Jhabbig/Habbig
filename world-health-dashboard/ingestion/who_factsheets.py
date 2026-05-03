"""WHO Fact-Sheets parser — rich disease records.

WHO maintains ~210 fact sheets at /news-room/fact-sheets/detail/<slug>. Each
has a stable structure with H2 headers (Overview, Symptoms, Treatment,
Prevention, etc.). We:

  1. Scrape the index page to get the list of slugs.
  2. Fetch each detail page (parallelised) and parse H2-delimited sections.
  3. Map WHO's section names to our canonical schema (causes, symptoms,
     transmission, treatment, prevention, etc.).
  4. Extract bullet-list "Key facts" when present.

There is no public API; the HTML format is stable but defensive parsing is
prudent. Cached for 24h on disk; re-parsing the full set takes ~30s.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)

INDEX_URL = "https://www.who.int/news-room/fact-sheets"
DETAIL_URL = "https://www.who.int/news-room/fact-sheets/detail/{slug}"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "who_factsheets"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_TTL_SECONDS = 24 * 3600
INDEX_CACHE_TTL = 7 * 24 * 3600  # weekly — list rarely changes

_lock = Lock()

# WHO H2 section name → our canonical key.
# Anything not in this map is preserved under "extra".
SECTION_MAP: dict[str, str] = {
    "key facts": "key_facts",
    "overview": "overview",
    "symptoms": "symptoms",
    "signs and symptoms": "symptoms",
    "transmission": "transmission",
    "causes": "causes",
    "risk factors": "risk_factors",
    "diagnosis": "diagnosis",
    "treatment": "treatment",
    "treatment and care": "treatment",
    "treatment and prevention": "treatment",
    "prevention": "prevention",
    "prevention and control": "prevention",
    "vaccines": "vaccines",
    "vaccine": "vaccines",
    "epidemiology": "epidemiology",
    "disease burden": "burden",
    "burden": "burden",
    "scope of the problem": "burden",
    "who response": "who_response",
}

# H2 sections WHO uses that aren't disease-specific or not interesting for the
# disease atlas — we drop these.
DROP_SECTIONS: set[str] = {
    "who response",
    "footnotes",
    "references",
    "related links",
    "media centre",
    "more information",
    "sources",
}


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.DOTALL | re.IGNORECASE)


def _strip(s: str, max_len: int = 2000) -> str:
    if not s:
        return ""
    txt = html.unescape(_TAG_RE.sub(" ", s))
    txt = _WS_RE.sub(" ", txt).strip()
    return txt[:max_len].rstrip() + ("…" if len(txt) > max_len else "")


def _bullets(html_block: str, max_items: int = 12) -> list[str]:
    """Extract <li> items as plain text from a chunk of HTML."""
    out: list[str] = []
    for m in _LI_RE.finditer(html_block):
        txt = _strip(m.group(1), max_len=240)
        if txt:
            out.append(txt)
        if len(out) >= max_items:
            break
    return out


def _http_get(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "world-health-dashboard/0.4",
            "Accept": "text/html",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted)
        return resp.read().decode("utf-8", errors="replace")


# ─── Index of fact sheets ───────────────────────────────────────────────────


def _index_cache_path() -> Path:
    return CACHE_DIR / "_index.json"


def fetch_index(force: bool = False) -> list[dict]:
    """Return list of {slug, name} for every WHO fact sheet."""
    p = _index_cache_path()
    if not force and p.exists():
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
            if (time.time() - body.get("fetched_at", 0)) < INDEX_CACHE_TTL:
                return body.get("items", [])
        except Exception as cache_exc:
            log.warning("who_factsheets index cache read failed (%s); will re-fetch", cache_exc)

    try:
        html_body = _http_get(INDEX_URL)
    except Exception as exc:
        log.warning("WHO factsheet index fetch failed: %s", exc)
        return []

    # Each link looks like:
    #   <a class="..." href="/news-room/fact-sheets/detail/SLUG">Display Name</a>
    pattern = re.compile(
        r'href="/news-room/fact-sheets/detail/([a-z0-9\-]+)"[^>]*>([^<]+)<',
        re.IGNORECASE,
    )
    seen: dict[str, str] = {}
    for m in pattern.finditer(html_body):
        slug = m.group(1)
        name = _strip(m.group(2), max_len=200)
        if slug and name and slug not in seen:
            seen[slug] = name

    items = [{"slug": s, "name": n, "source_url": DETAIL_URL.format(slug=s)} for s, n in seen.items()]
    items.sort(key=lambda x: x["name"].lower())
    p.write_text(json.dumps({"fetched_at": time.time(), "items": items}), encoding="utf-8")
    log.info("WHO factsheet index: %d entries", len(items))
    return items


# ─── Detail-page parser ─────────────────────────────────────────────────────


def _split_sections(article_html: str) -> dict[str, str]:
    """Split article HTML on <h2> tags; return {section_name: inner_html}."""
    out: dict[str, str] = {}
    h2_iter = list(re.finditer(r"<h2[^>]*>(.*?)</h2>", article_html, re.DOTALL | re.IGNORECASE))
    for i, m in enumerate(h2_iter):
        title = _strip(m.group(1), max_len=200).lower().strip()
        start = m.end()
        end = h2_iter[i + 1].start() if i + 1 < len(h2_iter) else len(article_html)
        out[title] = article_html[start:end]
    return out


def _parse_factsheet(name: str, slug: str, html_body: str) -> dict:
    # Locate the article body — WHO wraps content in <article> or
    # <div class="sf-detail-body-wrapper">. Fall back to whole document.
    body = ""
    m = re.search(r"<article[^>]*>(.*?)</article>", html_body, re.DOTALL | re.IGNORECASE)
    if m:
        body = m.group(1)
    else:
        m = re.search(r"<main[^>]*>(.*?)</main>", html_body, re.DOTALL | re.IGNORECASE)
        body = m.group(1) if m else html_body

    sections = _split_sections(body)

    # Key facts often appears in a side block before the article H2's. Try a
    # bullet list with "key facts" heading anywhere in the body.
    key_facts: list[str] = []
    kf_match = re.search(
        r"(?:Key\s+facts|KEY\s+FACTS)\s*</h[2-3]>\s*<ul[^>]*>(.*?)</ul>",
        body,
        re.DOTALL | re.IGNORECASE,
    )
    if kf_match:
        key_facts = _bullets(kf_match.group(1), max_items=10)
    elif "key facts" in sections:
        key_facts = _bullets(sections["key facts"], max_items=10)

    record: dict = {
        "slug": slug,
        "name": name,
        "source": "WHO",
        "source_url": DETAIL_URL.format(slug=slug),
        "key_facts": key_facts,
        "sections": {},
        "extra_sections": {},
    }

    for raw_name, body_html in sections.items():
        if raw_name in DROP_SECTIONS:
            continue
        canonical = SECTION_MAP.get(raw_name)
        text = _strip(body_html, max_len=3500)
        if not text:
            continue
        bullets = _bullets(body_html, max_items=15)
        if canonical:
            record["sections"][canonical] = {
                "title": raw_name.title(),
                "text": text,
                "bullets": bullets,
            }
        else:
            record["extra_sections"][raw_name] = text[:1500]

    return record


def _detail_cache_path(slug: str) -> Path:
    return CACHE_DIR / f"{slug}.json"


def _read_detail_cache(slug: str) -> dict | None:
    p = _detail_cache_path(slug)
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
        if (time.time() - body.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
            return body.get("record")
    except Exception:
        return None
    return None


def fetch_factsheet(slug: str, name: str | None = None, force: bool = False) -> dict | None:
    if not force:
        cached = _read_detail_cache(slug)
        if cached:
            return cached
    try:
        body = _http_get(DETAIL_URL.format(slug=slug))
    except Exception as exc:
        log.warning("WHO factsheet %s fetch failed: %s", slug, exc)
        # Try stale cache.
        p = _detail_cache_path(slug)
        if p.exists():
            try:
                stale = json.loads(p.read_text(encoding="utf-8")).get("record")
                if stale:
                    stale["stale"] = True
                    return stale
            except Exception as cache_exc:
                log.warning("who_factsheets stale cache read failed for %s (%s); returning None", slug, cache_exc)
        return None

    name = name or slug.replace("-", " ").title()
    rec = _parse_factsheet(name, slug, body)
    try:
        _detail_cache_path(slug).write_text(
            json.dumps({"fetched_at": time.time(), "record": rec}),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("WHO factsheet %s cache write failed: %s", slug, exc)
    return rec


def fetch_all(max_workers: int = 12, force: bool = False) -> list[dict]:
    index = fetch_index(force=force)
    out: list[dict] = []

    def _one(item: dict) -> dict | None:
        return fetch_factsheet(item["slug"], item["name"], force=force)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for rec in pool.map(_one, index):
            if rec:
                out.append(rec)
    log.info("WHO factsheets: %d/%d parsed", len(out), len(index))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    idx = fetch_index(force=True)
    print(f"index: {len(idx)} factsheets")
    # Spot-check malaria.
    rec = fetch_factsheet("malaria")
    print(f"\nMalaria sections: {sorted(rec['sections'].keys())}")
    print(f"Key facts ({len(rec['key_facts'])}):")
    for kf in rec["key_facts"][:5]:
        print(f"  • {kf[:100]}")
    if "treatment" in rec["sections"]:
        print(f"\nTreatment (first 200 chars): {rec['sections']['treatment']['text'][:200]}")
    if "transmission" in rec["sections"]:
        print(f"\nTransmission: {rec['sections']['transmission']['text'][:200]}")
