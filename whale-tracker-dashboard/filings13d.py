"""SC 13D / 13G parser.

Schedule 13D is filed within 10 days of acquiring >5% of a public company
with intent to influence — the activist signal. 13G is the passive variant
(index funds, long-term holders); we still surface it because some
activists (Buffett) have famously used 13G.

The filing's primary HTML is unstructured prose, so we extract a few
fields with regexes:
  - issuer name (cover page "Name of Issuer")
  - filer name  (cover page "Name of Filing Person")
  - aggregate amount beneficially owned (Item 11)
  - percent of class (Item 13)
  - CUSIP (Item 2(d))

We don't try to map CUSIP→ticker here; the dashboard surfaces issuer
name + (when EDGAR provides) ticker from the filing-index metadata.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("filings13d")

_PCT_PATTERNS = [
    re.compile(r"percent\s+of\s+class\s+represented\s+by\s+amount\s+in\s+row\s*\(?11\)?\s*[:\s]*([\d.]+)", re.I),
    re.compile(r"percent\s+of\s+class\s*[:\s]+([\d.]+)\s*%", re.I),
    re.compile(r"approximately\s+([\d.]+)\s*%\s+of\s+the\s+(outstanding|issued)", re.I),
]

_SHARES_PATTERNS = [
    re.compile(r"aggregate\s+amount\s+beneficially\s+owned\s+by\s+each\s+reporting\s+person\s*[:\s]*([\d,]+)", re.I),
    re.compile(r"\b([\d,]{4,})\s+shares\s+of\s+(common\s+stock|class\s+a)", re.I),
]

_ISSUER_PATTERNS = [
    re.compile(r"name\s+of\s+issuer\s*[:\s]+([A-Z0-9 ,&.'/()\-]+)\s*$", re.I | re.M),
    re.compile(r"the\s+issuer\s+is\s+([A-Z][A-Za-z0-9 ,&.'/()\-]{3,})", re.I),
]


def parse_13_filing(text: str) -> dict:
    """Extract a small set of fields from a 13D/G primary doc body.

    Input is the raw HTML body — we strip tags first.
    """
    body = _strip_html(text)

    pct = _first_float(body, _PCT_PATTERNS)
    shares = _first_int(body, _SHARES_PATTERNS)
    issuer_name = _first_str(body, _ISSUER_PATTERNS)

    return {
        "pct_owned":    pct,
        "shares_owned": shares,
        "issuer_name_extracted": issuer_name,
    }


def _strip_html(s: str) -> str:
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style[^>]*>.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&amp;", "&", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _first_float(body: str, patterns: list[re.Pattern]) -> float | None:
    for p in patterns:
        m = p.search(body)
        if m:
            try:
                return float(m.group(1))
            except (ValueError, IndexError):
                continue
    return None


def _first_int(body: str, patterns: list[re.Pattern]) -> float | None:
    for p in patterns:
        m = p.search(body)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except (ValueError, IndexError):
                continue
    return None


def _first_str(body: str, patterns: list[re.Pattern]) -> str:
    for p in patterns:
        m = p.search(body)
        if m:
            return m.group(1).strip()
    return ""
