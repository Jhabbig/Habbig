"""Legislative-bills tracker — v2.3.

Pulls bill / legislative-procedure feeds from major financial-regulator
jurisdictions and surfaces those that touch financial regulation. Gives
early warning on regulatory change before it lands as an actual
agency rulemaking.

Sources:
  - US Congress (most recent actions, financial-services scoped)
  - EU Parliament legislative procedures (ECON/JURI committee tracked)
  - UK Parliament public bills

Reuses the v0 `_rss.py` parser. Filter is verb × topic conjunction
(same shape as `parliament_hearings.py`): a bill matches when title
or summary indicates **action on a bill** AND **financial-regulator
relevance**. Either alone is too broad — "Introduction of HR 1234"
has the verb-noun but not necessarily the topic; "FCA mention in
press release" has the topic but isn't a bill action.

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
        code="US-CONGRESS",
        name="U.S. Congress — most recent bill actions (financial services)",
        jurisdiction="US",
        # Best-guess feed; Congress.gov has multiple RSS endpoints under
        # /rss. If this 404s, lift the right URL from
        # https://www.congress.gov/rss and drop it here.
        rss_url="https://www.congress.gov/rss/most-recent-bills.xml",
    ),
    RssSource(
        code="UK-BILLS",
        name="UK Parliament — public bills",
        jurisdiction="UK",
        rss_url="https://bills.parliament.uk/rss/allbills.rss",
    ),
    RssSource(
        code="EU-PROC",
        name="EU Parliament — legislative procedures (ECON-relevant)",
        jurisdiction="EU",
        # Best-guess — EUR-Lex / EP publishes legislative-procedure feeds
        # under various paths that rotate annually.
        rss_url="https://oeil.secure.europarl.europa.eu/oeil/rss/feed-recent-byCommittee.xml?committee=ECON",
    ),
]


_VERB_RX = re.compile(
    r"\b("
    r"introduce[d]?|introducing|introduction|"
    r"first\s+reading|second\s+reading|third\s+reading|"
    r"committee\s+(stage|report)|royal\s+assent|"
    r"passed|enacted|signed\s+into\s+law|"
    r"reported\s+out|reported\s+favorably|"
    r"adopted|approved|vote[d]?|"
    r"amendment|amended|markup|"
    r"H\.R\.\s*\d+|S\.\s*\d+|"  # US bill numbers
    r"H\.B\.\s*\d+|S\.B\.\s*\d+|"
    r"bill\s+\d+"
    r")\b",
    re.IGNORECASE,
)

_TOPIC_RX = re.compile(
    r"\b("
    # Regulator codes
    r"SEC|CFTC|FinCEN|OCC|FDIC|CFPB|OFAC|Federal\s+Reserve|Treasury|"
    r"FCA|PRA|BoE|Bank\s+of\s+England|"
    r"ESMA|EBA|EIOPA|ECB|"
    # Subject matter
    r"securities|financial\s+services|financial\s+sector|"
    r"banking|capital\s+markets?|prudential|"
    r"insurance|pensions|consumer\s+credit|consumer\s+duty|"
    r"crypto|stablecoin|digital\s+asset|MiCA|MiFID|"
    r"money\s+laundering|AML|sanctions|terrorist\s+financing|"
    r"market\s+structure|payment\s+for\s+order\s+flow|PFOF|"
    r"investment\s+adviser|private\s+fund|hedge\s+fund|"
    r"cybersecurity|data\s+protection"
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
            raw = fetch_source(src, max_items=80, since_days=180)
            sources_status.append({
                "code": src.code, "name": src.name,
                "jurisdiction": src.jurisdiction, "rss_url": src.rss_url,
                "ok": True, "count": len(raw), "error": None,
            })
        except Exception as exc:
            log.warning("Bill source %s failed: %s", src.code, exc)
            sources_status.append({
                "code": src.code, "name": src.name,
                "jurisdiction": src.jurisdiction, "rss_url": src.rss_url,
                "ok": False, "count": 0, "error": str(exc),
            })
            continue
        for it in raw:
            if _is_relevant(it):
                items.append({**it, "chamber": src.code, "chamber_name": src.name})
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
        ("H.R. 1234 introduced — Stablecoin Oversight Act", True),
        ("Third reading of Financial Services and Markets Bill", True),
        ("Committee markup of SEC reform proposal", True),
        ("Bill 4567 amended to expand AML enforcement", True),
        ("Reported favorably: cybersecurity disclosure bill", True),
        ("First reading: Health Care Modernization Act", False),
        ("Education bill passed third reading", False),
        ("Vote on transportation infrastructure", False),
        ("Royal Assent for Online Safety Act", False),
        ("MiCA amendment adopted in second reading", True),
    ]
    pass_count = 0
    for title, expected in cases:
        got = _is_relevant({"title": title, "summary": ""})
        ok = got == expected
        pass_count += int(ok)
        mark = "✓" if ok else "✗"
        print(f"{mark} relevant={got!s:5s} (want {expected!s:5s})  | {title}")
    print(f"\n{pass_count}/{len(cases)} fixtures pass")
