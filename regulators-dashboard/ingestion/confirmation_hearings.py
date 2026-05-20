"""Confirmation-hearing tracker — v1.1.

Pulls hearing feeds from the Senate Banking Committee and the House
Financial Services Committee, filters to items whose title or summary
indicates a nomination / confirmation hearing, and exposes the matched
hearings as a normalized list.

This complements `data/personnel.py` rather than replacing it: personnel
covers *confirmed* officials with their term-end anchors; this module
covers the *pending* pipeline (who's been nominated, hearing dates).

The feed URLs below are best-guess against the common Senate / House
RSS conventions. Both chambers re-organize their feed paths roughly
annually — if a source goes red in `/api/hearings`, drop in the
current URL (lift from the committee homepage) without touching code.

Reuses the v0 `_rss.py` parser so we get defusedxml safety and the same
graceful-degradation behavior as the regulator RSS sources.

Cache: 1 h. Hearings are scheduled days-to-weeks ahead; 5-min polling
doesn't surface anything new.
"""

from __future__ import annotations

import logging
import re
import time
from threading import Lock

from ._rss import RssSource, fetch_source

log = logging.getLogger(__name__)

# Best-guess feed URLs. Replace with the live URL from each committee
# homepage if the candidate below 404s — graceful degradation means a
# wrong URL just renders the source as `unavailable` in `/api/hearings`.
SOURCES: list[RssSource] = [
    RssSource(
        code="SBC",
        name="Senate Banking, Housing, and Urban Affairs Committee",
        jurisdiction="US",
        rss_url="https://www.banking.senate.gov/rss/feed",
        title_filter=None,
    ),
    RssSource(
        code="HFS",
        name="House Financial Services Committee",
        jurisdiction="US",
        rss_url="https://financialservices.house.gov/news/documentquery.aspx?DocumentTypeID=2685&_=1&output=rss",
        title_filter=None,
    ),
]

# A hearing/item matches if any of these patterns hits the title or
# summary. Keep this list narrow — false positives bury the signal.
_CONFIRMATION_RX = re.compile(
    r"\b("
    r"nomination|nominat(ed|es|ing)|nominee|nominees|"
    r"confirmation\s+hearing|"
    r"to\s+be\s+(the\s+)?(chair|chairman|chairperson|commissioner|governor|director|secretary)|"
    r"hearing\s+on\s+the\s+nomination"
    r")\b",
    re.IGNORECASE,
)

# Regulator codes we care about — used to extract a hint when present.
_REGULATOR_HINTS = (
    ("SEC", re.compile(r"\bSEC\b|Securities\s+and\s+Exchange\s+Commission", re.I)),
    ("Fed", re.compile(r"Federal\s+Reserve|FOMC", re.I)),
    ("CFTC", re.compile(r"\bCFTC\b|Commodity\s+Futures", re.I)),
    ("FDIC", re.compile(r"\bFDIC\b|Federal\s+Deposit", re.I)),
    ("OCC", re.compile(r"\bOCC\b|Comptroller\s+of\s+the\s+Currency", re.I)),
    ("FinCEN", re.compile(r"\bFinCEN\b|Financial\s+Crimes", re.I)),
    ("OFAC", re.compile(r"\bOFAC\b|Foreign\s+Assets\s+Control", re.I)),
    ("CFPB", re.compile(r"\bCFPB\b|Consumer\s+Financial\s+Protection", re.I)),
    ("HUD",  re.compile(r"\bHUD\b|Housing\s+and\s+Urban", re.I)),
    ("Treasury", re.compile(r"\bTreasury\b", re.I)),
)


def _regulator_hint(text: str) -> str | None:
    for code, rx in _REGULATOR_HINTS:
        if rx.search(text):
            return code
    return None


def _is_confirmation(it: dict) -> bool:
    text = f"{it.get('title', '')} {it.get('summary', '')}"
    return bool(_CONFIRMATION_RX.search(text))


_CACHE_TTL = 60 * 60
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_lock = Lock()


def _fetch_all() -> dict:
    out_items: list[dict] = []
    sources_status: list[dict] = []
    for src in SOURCES:
        try:
            items = fetch_source(src, max_items=50, since_days=120)
            sources_status.append({
                "code": src.code,
                "name": src.name,
                "rss_url": src.rss_url,
                "ok": True,
                "count": len(items),
                "error": None,
            })
        except Exception as exc:
            log.warning("Hearings source %s failed: %s", src.code, exc)
            sources_status.append({
                "code": src.code,
                "name": src.name,
                "rss_url": src.rss_url,
                "ok": False,
                "count": 0,
                "error": str(exc),
            })
            continue
        for it in items:
            if not _is_confirmation(it):
                continue
            text = f"{it.get('title', '')} {it.get('summary', '')}"
            out_items.append({
                **it,
                "committee": src.code,
                "committee_name": src.name,
                "regulator_hint": _regulator_hint(text),
            })

    out_items.sort(key=lambda x: x.get("published") or "", reverse=True)
    return {"items": out_items, "sources": sources_status, "count": len(out_items)}


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
    # Unit-test the filter + hint logic against fixtures (no network).
    cases = [
        ("Hearing on the Nomination of John Doe to be SEC Chair", True, "SEC"),
        ("Nomination hearing for Jane Smith, Federal Reserve Governor", True, "Fed"),
        ("Confirmation hearing on CFTC Commissioner nominees", True, "CFTC"),
        ("Hearing on banking regulation policy", False, None),
        ("Markup of S.1234 — Banking Reform Act", False, None),
        ("To be Director of FinCEN: Q&A with the nominee", True, "FinCEN"),
        ("Roundtable on cryptocurrency oversight", False, None),
    ]
    pass_count = 0
    for title, expected_match, expected_hint in cases:
        item = {"title": title, "summary": ""}
        got_match = _is_confirmation(item)
        got_hint = _regulator_hint(title)
        ok = got_match == expected_match and got_hint == expected_hint
        pass_count += int(ok)
        mark = "✓" if ok else "✗"
        print(f"{mark} match={got_match!s:5s} (want {expected_match!s:5s})  "
              f"hint={got_hint!s:8s} (want {expected_hint!s:8s})  | {title}")
    print(f"\n{pass_count}/{len(cases)} fixtures pass")
