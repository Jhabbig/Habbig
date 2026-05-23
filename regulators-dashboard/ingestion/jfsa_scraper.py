"""JFSA (Japan FSA) HTML scraper — v2.2.

Japan's Financial Services Agency publishes news as plain HTML at
https://www.fsa.go.jp/en/news/ — no public RSS as of this writing.
This module fetches the listing page and pattern-matches `<a>` tags
near date stamps to extract press-release items, producing the same
normalized dict shape as `_rss.fetch_source`.

Design intent: **prove the pipeline handles non-RSS sources** so we
have a template for any future jurisdiction that publishes only HTML.
The same `SOURCE` + `fetch()` contract that the RSS shims expose lets
`unified_feed` mix RSS and scraped sources without special-casing.

Robustness notes:
  - Government sites rotate their markup on annual redesigns. Pattern
    here is conservative — multiple regexes try in order, first one
    that surfaces ≥ 1 item wins. If JFSA redesigns, drop a new pattern
    into `_PATTERNS` and ship.
  - All bytes-level decoding happens via `errors="replace"` so a
    weird-encoded glyph never breaks the whole fetch.
  - HTML is parsed with `defusedxml` lenient mode? No — we stick to
    regex parsing here. Defusedxml's HTML support is partial, and the
    risk surface for arbitrary regulator HTML is text-extraction
    (no script eval), so regex is the simpler safe choice.

Cache: 1 h, same as the RSS sources.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.request
from threading import Lock

from ._rss import RssSource, _strip_html

log = logging.getLogger(__name__)


SOURCE = RssSource(
    code="JFSA",
    name="Financial Services Agency (Japan) — scraped",
    jurisdiction="JP",
    rss_url="https://www.fsa.go.jp/en/news/",  # HTML, not RSS — see module docstring
)


UA = "regulators-dashboard/2.2 (+jfsa-scraper)"


def _fetch_bytes(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read(5_000_000)


# Pattern set — first match-yielding pattern wins. Each pattern must
# capture three groups: (date, link, title). Dates accepted in either
# YYYY-MM-DD or YYYY/MM/DD form; we normalize to ISO.
#
# Pattern 1: classic JFSA layout — <dt>YYYY-MM-DD</dt><dd><a href="…">Title</a>…</dd>
# Pattern 2: <li> with separate date and link
# Pattern 3: anchor-first with trailing date — common after redesigns
_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"""<d[td]>\s*(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})[^<]*</d[td]>\s*"""
        r"""<d[td]>\s*<a[^>]*href=["'](?P<link>[^"']+)["'][^>]*>(?P<title>[^<]+)</a>""",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"""<li[^>]*>\s*(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})[^<]*"""
        r"""<a[^>]*href=["'](?P<link>[^"']+)["'][^>]*>(?P<title>[^<]+)</a>""",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"""<a[^>]*href=["'](?P<link>[^"']+)["'][^>]*>(?P<title>[^<]+)</a>[^<]*"""
        r"""(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})""",
        re.IGNORECASE | re.DOTALL,
    ),
]


_BASE = "https://www.fsa.go.jp"


def _normalize_date(s: str) -> str:
    """Normalize YYYY-MM-DD or YYYY/MM/DD to ISO 8601 with UTC midnight."""
    s = s.replace("/", "-").strip()
    parts = s.split("-")
    if len(parts) != 3:
        return ""
    try:
        y, m, d = (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}T00:00:00+00:00"


def _absolute(link: str) -> str:
    if link.startswith(("http://", "https://")):
        return link
    if link.startswith("/"):
        return f"{_BASE}{link}"
    return f"{_BASE}/en/news/{link.lstrip('./')}"


def parse_html(html: str, max_items: int = 50) -> list[dict]:
    """Return up to `max_items` normalized item dicts. Tries each pattern
    in order; the first one yielding ≥ 1 result wins so we don't blend
    parses from incompatible layouts."""
    out: list[dict] = []
    for pat in _PATTERNS:
        out = []
        for m in pat.finditer(html):
            title = _strip_html(m.group("title")).strip()
            link = _absolute(m.group("link").strip())
            date_iso = _normalize_date(m.group("date"))
            if not title or not link:
                continue
            out.append({
                "id": f"{SOURCE.code}::{link}",
                "source": SOURCE.code,
                "source_name": SOURCE.name,
                "jurisdiction": SOURCE.jurisdiction,
                "title": title,
                "link": link,
                "summary": "",  # JFSA listing page has titles only
                "published": date_iso,
                "tags": [],
            })
            if len(out) >= max_items:
                break
        if out:
            return out
    return out


def fetch(max_items: int = 50, since_days: int | None = 90) -> list[dict]:
    """Fetch + scrape the JFSA listing page. Caller via `unified_feed`
    handles the per-source try/except so an HTTP failure just yields []."""
    body = _fetch_bytes(SOURCE.rss_url)
    text = body.decode("utf-8", errors="replace")
    items = parse_html(text, max_items=max_items)
    # Apply since_days filter
    if since_days is not None and items:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        kept: list[dict] = []
        for it in items:
            try:
                dt = datetime.fromisoformat(it["published"])
                if dt >= cutoff:
                    kept.append(it)
            except ValueError:
                kept.append(it)  # keep undated items
        items = kept
    return items


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    # Synthetic HTML fixtures — no network. Validates each pattern can parse
    # at least one item out and the normalizer assembles a sane dict.
    fixtures = [
        # Pattern 1: <dt>date</dt><dd><a>title</a></dd>
        ("<dl><dt>2026-05-15</dt><dd><a href='/en/news/2026/20260515-1.html'>"
         "Administrative action against a securities firm</a></dd></dl>"),
        # Pattern 2: <li>date<a>title</a></li>
        ("<ul><li>2026/05/10 <a href='/en/news/2026/20260510-2.html'>"
         "Public consultation on stablecoin guidance</a></li></ul>"),
        # Pattern 3: anchor-first, date trailing
        ("<div><a href='https://www.fsa.go.jp/en/news/2026/20260501.html'>"
         "Notice on capital adequacy</a> 2026-05-01</div>"),
        # Mixed garbage that shouldn't match anything
        ("<html><body><p>Welcome to FSA</p></body></html>"),
    ]
    pass_count = 0
    for idx, html in enumerate(fixtures, 1):
        got = parse_html(html, max_items=10)
        if idx < 4:
            ok = len(got) == 1 and got[0]["title"] and got[0]["link"].startswith("https://")
        else:
            ok = got == []
        pass_count += int(ok)
        mark = "✓" if ok else "✗"
        if got:
            print(f"{mark} fixture {idx}: {got[0]['published'][:10]}  {got[0]['title']}")
        else:
            print(f"{mark} fixture {idx}: (no items, as expected)" if idx == 4
                  else f"{mark} fixture {idx}: NO ITEMS PARSED")
    print(f"\n{pass_count}/{len(fixtures)} fixtures pass")
