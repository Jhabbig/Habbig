"""Court-case feed tracker — v2.0.

Pulls free, RSS-shaped court decision feeds from a small set of
jurisdictions that publish them publicly. Each item is a *decided* or
*newly listed* case — judgments, orders, opinions.

Scope is deliberately narrow:
  - Free, RSS/Atom feeds only. No PACER, no per-jurisdiction HTML
    scraping, no paid legal-database APIs.
  - Financial / securities / sanctions cases are the editorial focus
    — items get keyword-filtered against `_RELEVANCE_RX` so generic
    civil litigation (family, immigration, etc.) doesn't drown the
    signal.
  - Same graceful-degradation lane as `confirmation_hearings`: a
    bad URL just renders `ok=False` for that source.

To extend:
  - Add a new `_RawSource` to `SOURCES`.
  - If a court system has no public RSS, write a dedicated scraper
    module instead (mirror `ingestion/ofac_sdn.py`).

Cache: 1 h — court systems publish daily at most.
"""

from __future__ import annotations

import logging
import re
import time
from threading import Lock

from ._rss import RssSource, fetch_source

log = logging.getLogger(__name__)


SOURCES: list[RssSource] = [
    RssSource(
        code="CJEU",
        name="Court of Justice of the European Union — recent case law",
        jurisdiction="EU",
        # EUR-Lex publishes weekly. Drop a more current feed URL here if the
        # one below has been rotated.
        rss_url="https://eur-lex.europa.eu/EN/display-feed.rss?myRssId=NEW_CASE_LAW&lang=en",
    ),
    RssSource(
        code="UK-COURTS",
        name="UK judiciary decisions",
        jurisdiction="UK",
        # National Archives publishes judgments under the Find Case Law
        # service; rotate the URL here if upstream changes.
        rss_url="https://caselaw.nationalarchives.gov.uk/atom.xml",
    ),
    RssSource(
        code="SCOTUS",
        name="U.S. Supreme Court — opinions of the court",
        jurisdiction="US",
        rss_url="https://www.supremecourt.gov/rss/cases.xml",
    ),
]


# Keep only items whose title/summary mentions a financial / regulatory /
# sanctions term. This protects against pulling the entire CJEU docket
# (which spans state aid, immigration, tax, IP, etc.) into a dashboard
# that's editorially about financial regulators.
_RELEVANCE_RX = re.compile(
    r"\b("
    r"securities|investor|fund|ETF|crypto|stablecoin|exchange|broker|"
    r"insider\s+trading|market\s+abuse|manipulation|fraud|disclosure|"
    r"AML|anti-money\s+laundering|sanctions|OFAC|banking|capital|"
    r"insurance|MiFID|MiCA|prospectus|short\s+selling|short-selling|"
    r"derivative|swap|hedge\s+fund|private\s+fund|asset\s+manager|asset\s+management"
    r")\b",
    re.IGNORECASE,
)


def _is_relevant(it: dict) -> bool:
    text = f"{it.get('title', '')} {it.get('summary', '')}"
    return bool(_RELEVANCE_RX.search(text))


_CACHE_TTL = 60 * 60
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_lock = Lock()


def _fetch_all() -> dict:
    items: list[dict] = []
    sources_status: list[dict] = []
    for src in SOURCES:
        try:
            raw = fetch_source(src, max_items=50, since_days=120)
            sources_status.append({
                "code": src.code, "name": src.name,
                "jurisdiction": src.jurisdiction, "rss_url": src.rss_url,
                "ok": True, "count": len(raw), "error": None,
            })
        except Exception as exc:
            log.warning("Court source %s failed: %s", src.code, exc)
            sources_status.append({
                "code": src.code, "name": src.name,
                "jurisdiction": src.jurisdiction, "rss_url": src.rss_url,
                "ok": False, "count": 0, "error": str(exc),
            })
            continue
        for it in raw:
            if _is_relevant(it):
                items.append({**it, "court": src.code, "court_name": src.name})
    items.sort(key=lambda x: x.get("published") or "", reverse=True)
    return {"items": items, "sources": sources_status, "count": len(items)}


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = _CACHE["data"] is not None and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        if fresh and not force:
            return _CACHE["data"]
    payload = _fetch_all()
    payload["fetched_at"] = now
    with _lock:
        _CACHE["data"] = payload
        _CACHE["fetched_at"] = now
    return payload


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    # Relevance filter fixtures (no network).
    cases = [
        ("Decision on insider trading appeal", True),
        ("Judgment in MiFID II prospectus case", True),
        ("Crypto exchange convicted of AML violations", True),
        ("Custody dispute in family law case", False),
        ("Immigration appeal dismissed", False),
        ("Patent infringement ruling — chip design", False),
        ("OFAC sanctions challenge denied", True),
        ("Class action against asset manager certified", True),
    ]
    pass_count = 0
    for title, expected in cases:
        got = _is_relevant({"title": title, "summary": ""})
        ok = got == expected
        pass_count += int(ok)
        mark = "✓" if ok else "✗"
        print(f"{mark} relevant={got!s:5s} (want {expected!s:5s})  | {title}")
    print(f"\n{pass_count}/{len(cases)} fixtures pass")
