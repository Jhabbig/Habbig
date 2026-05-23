"""Non-US parliament hearing tracker — v2.2.

Complements `confirmation_hearings.py` (US Senate Banking + House
Financial Services) with the equivalent committees in other major
jurisdictions:

  - UK Treasury Select Committee
  - UK Public Accounts Committee (regulator accountability)
  - EU Parliament — ECON (Economic and Monetary Affairs)
  - EU Parliament — JURI (Legal Affairs, often touches regulator topics)

The filter regex is broader than the US confirmation tracker because
European parliament committees publish a wider mix of activity:
appointments, structured dialogues with regulators, evidence sessions,
bill scrutiny, and inquiries. We keep only items that mention a
financial regulator, hearing/dialogue/inquiry verbs, or appointment
verbs.

Reuses the v0 `_rss.py` parser. Same graceful-degradation lane as every
other RSS module: a 404'd URL surfaces as `ok=false` in `/api/parliament`.

Cache: 1 h.
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
        code="UK-TSC",
        name="UK House of Commons — Treasury Committee",
        jurisdiction="UK",
        rss_url="https://committees.parliament.uk/committee/158/treasury-committee/news/feed",
    ),
    RssSource(
        code="UK-PAC",
        name="UK House of Commons — Public Accounts Committee",
        jurisdiction="UK",
        rss_url="https://committees.parliament.uk/committee/127/public-accounts-committee/news/feed",
    ),
    RssSource(
        code="EU-ECON",
        name="European Parliament — Economic and Monetary Affairs (ECON)",
        jurisdiction="EU",
        rss_url="https://www.europarl.europa.eu/committees/en/econ/home/rss-news",
    ),
    RssSource(
        code="EU-JURI",
        name="European Parliament — Legal Affairs (JURI)",
        jurisdiction="EU",
        rss_url="https://www.europarl.europa.eu/committees/en/juri/home/rss-news",
    ),
]

# An item is parliament-hearing-relevant if BOTH:
#   (a) it mentions a hearing / appointment / evidence verb, AND
#   (b) the topic is financial-regulator-relevant.
# Either alone is too broad — "Inquiry into transport infrastructure"
# has the verb but isn't our editorial lane, and a generic "FCA" tweet
# without hearing context isn't a hearing.
_VERB_RX = re.compile(
    r"\b("
    r"appointment|appointed|nomination|nominee|confirmation|"
    r"evidence\s+session|structured\s+dialogue|exchange\s+of\s+views|"
    r"oral\s+evidence|hearing\s+with|hearing\s+on|inquiry\s+into|review\s+of|"
    r"scrutiny\s+of|examination\s+of"
    r")\b",
    re.IGNORECASE,
)

_TOPIC_RX = re.compile(
    r"\b("
    # Regulator codes / names
    r"FCA|PRA|BoE|Bank\s+of\s+England|"
    r"ESMA|EBA|EIOPA|ECB|"
    r"Single\s+Resolution|SRB|"
    # Subject matter
    r"banking|capital\s+markets?|securities|prudential|"
    r"insurance|pensions|consumer\s+credit|consumer\s+duty|"
    r"crypto|stablecoin|MiCA|MiFID|"
    r"financial\s+services|financial\s+sector|"
    r"money\s+laundering|AML|sanctions"
    r")\b",
    re.IGNORECASE,
)


def _is_relevant(it: dict) -> bool:
    text = f"{it.get('title', '')} {it.get('summary', '')}"
    return bool(_VERB_RX.search(text)) and bool(_TOPIC_RX.search(text))


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
            log.warning("Parliament source %s failed: %s", src.code, exc)
            sources_status.append({
                "code": src.code, "name": src.name,
                "jurisdiction": src.jurisdiction, "rss_url": src.rss_url,
                "ok": False, "count": 0, "error": str(exc),
            })
            continue
        for it in raw:
            if _is_relevant(it):
                items.append({**it, "committee": src.code, "committee_name": src.name})
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
    cases = [
        # (title, expected_relevance)
        ("Treasury Committee evidence session with the FCA", True),
        ("Structured dialogue with the ECB at ECON", True),
        ("Public Accounts Committee inquiry into banking regulation", True),
        ("Appointment of new Governor of the Bank of England", True),
        ("Exchange of views on MiCA with ESMA Chair", True),
        ("Committee discussion of farming subsidies", False),
        ("Inquiry into transport infrastructure", False),
        ("Debate on educational reform", False),
        ("Oral evidence on consumer credit", True),
        ("JURI committee on intellectual property", False),
    ]
    pass_count = 0
    for title, expected in cases:
        got = _is_relevant({"title": title, "summary": ""})
        ok = got == expected
        pass_count += int(ok)
        mark = "✓" if ok else "✗"
        print(f"{mark} relevant={got!s:5s} (want {expected!s:5s})  | {title}")
    print(f"\n{pass_count}/{len(cases)} fixtures pass")
