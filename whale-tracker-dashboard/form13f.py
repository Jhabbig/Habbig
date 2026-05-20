"""Form 13F-HR INFORMATION TABLE parser.

A 13F filing has two structured XML attachments:
  - the "primary doc" (cover page metadata, total value, holding count, manager info)
  - the INFORMATION TABLE — one <infoTable> entry per holding line.

We parse the information table to get per-issuer rows: CUSIP, value,
shares, put/call. The cover page would let us cross-check the total
value; we just sum the lines.

A note on units: 13F filings have used "thousands of dollars" historically
but the SEC moved to dollar-precision reporting in some windows. We store
the raw `<value>` as reported and let the README warn the reader. The
relative ranking within a single filing is always correct regardless of
unit.
"""

from __future__ import annotations

import logging
import re
from xml.etree import ElementTree as ET

log = logging.getLogger("form13f")


def _local(tag: str) -> str:
    """Strip XML namespace for fragile comparisons."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _text(el, name: str, default: str = "") -> str:
    if el is None:
        return default
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return default


def _child(el, name):
    if el is None:
        return None
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _float(s: str) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


def parse_information_table(xml: str, *, accession: str, fund_cik: str, period_of_report: str) -> list[dict]:
    """Return a list of fund_holding rows from a 13F INFORMATION TABLE XML body."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.warning("13F XML parse error for %s: %s", accession, e)
        return []

    rows: list[dict] = []
    line_no = 0
    for entry in root:
        if _local(entry.tag) != "infoTable":
            continue
        line_no += 1
        cusip = _text(entry, "cusip").upper()
        issuer_name = _text(entry, "nameOfIssuer")
        title_class = _text(entry, "titleOfClass")
        value = _float(_text(entry, "value"))
        put_call = _text(entry, "putCall") or None

        shares_block = _child(entry, "shrsOrPrnAmt")
        shares = _float(_text(shares_block, "sshPrnamt"))
        shares_type = _text(shares_block, "sshPrnamtType") or None

        rows.append({
            "accession":      accession,
            "line_no":        line_no,
            "fund_cik":       fund_cik,
            "period_of_report": period_of_report,
            "cusip":          cusip or None,
            "issuer_name":    issuer_name or None,
            "title_of_class": title_class or None,
            "issuer_ticker":  None,  # populated downstream via CUSIP→ticker if available
            "value":          value,
            "shares":         shares,
            "shares_type":    shares_type,
            "put_call":       put_call,
        })

    return rows


_PERIOD_RX = re.compile(r"(\d{4}-\d{2}-\d{2})")


def extract_period_of_report(primary_xml: str) -> str:
    """Best-effort extract of `periodOfReport` from the primary document.

    The primary doc XML has a `<periodOfReport>03-31-2025</periodOfReport>`
    or sometimes `<periodOfReport>2025-03-31</periodOfReport>`. We tolerate
    both.
    """
    if not primary_xml:
        return ""
    try:
        root = ET.fromstring(primary_xml)
    except ET.ParseError:
        return ""

    def walk(el):
        if _local(el.tag) == "periodOfReport":
            return (el.text or "").strip()
        for c in el:
            v = walk(c)
            if v:
                return v
        return ""

    raw = walk(root)
    if not raw:
        return ""
    # Normalise MM-DD-YYYY → YYYY-MM-DD
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})$", raw)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    m = _PERIOD_RX.search(raw)
    return m.group(1) if m else raw


def extract_fund_name(primary_xml: str) -> str:
    if not primary_xml:
        return ""
    try:
        root = ET.fromstring(primary_xml)
    except ET.ParseError:
        return ""

    def walk(el, target):
        if _local(el.tag) == target:
            return (el.text or "").strip()
        for c in el:
            v = walk(c, target)
            if v:
                return v
        return ""

    return walk(root, "filingManager") or walk(root, "name") or ""
