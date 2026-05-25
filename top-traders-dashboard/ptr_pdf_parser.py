#!/usr/bin/env python3
"""
House PTR PDF parser.

The official House Clerk XML index gives us "Member X filed PTR Y on date Z"
but no ticker / side / amount — those live inside the linked PDF. This module
shells out to `pdftotext -layout` (poppler, already on the prod box) and
regex-parses the resulting columnar text into transaction dicts.

The Clerk's PTR template is stable: each transaction is a multi-line block
that starts with an Owner code (SP / JT / DC / blank) followed by the asset
description, transaction type, dates, and amount range.

Example block (after `pdftotext -layout`):

    SP    Alphabet Inc. - Class A Common           P    01/16/2026 01/16/2026  $500,001 -
          Stock (GOOGL) [ST]                                                   $1,000,000
          F     S       : New
          D           : Exercised 50 call options …

Fields parsed:
  owner          'SP' | 'JT' | 'DC' | ''
  asset_name     'Alphabet Inc. - Class A Common Stock'
  ticker         'GOOGL'
  asset_type     'ST' (stock) / 'OP' (options) / 'AB' / etc
  tx_type        'P' | 'S' | 'S (partial)' | 'E'
  tx_date_iso    '2026-01-16'
  notif_date_iso '2026-01-16'
  amount_low     500001.0
  amount_high    1000000.0
  description    'Exercised 50 call options …'

Caller maps tx_type → side ('buy' / 'sell' / 'exchange').

Two failure modes degrade gracefully:
  - PDF download fails → return []
  - pdftotext exits non-zero or text doesn't match the template → return []
The caller (congress_ptr.enrich_house_filings) keeps the filing-level row
as a fallback so the actor at least appears in the unified feed.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

logger = logging.getLogger(__name__)

PDFTOTEXT_BIN = shutil.which("pdftotext")
HTTP_TIMEOUT = 20.0
USER_AGENT = "narve-insider-tracker (research; contact via narve.ai)"


def is_available() -> bool:
    """True if pdftotext is on PATH (poppler-utils installed)."""
    return PDFTOTEXT_BIN is not None


# ─── PDF → text ──────────────────────────────────────────────────────

def _fetch_pdf(url: str) -> bytes | None:
    if not url:
        return None
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as c:
            r = c.get(url)
            if r.status_code != 200:
                logger.debug("PTR PDF fetch %s: HTTP %d", url, r.status_code)
                return None
            return r.content
    except Exception as e:
        logger.debug("PTR PDF fetch %s failed: %s", url, e)
        return None


def _pdf_to_text(pdf_bytes: bytes) -> str | None:
    """Run `pdftotext -layout - -` on the bytes. Returns text or None on failure."""
    if not is_available():
        return None
    try:
        # Write to temp file because some pdftotext builds choke on stdin streams.
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            tmp.write(pdf_bytes)
            tmp.flush()
            res = subprocess.run(
                [PDFTOTEXT_BIN, "-layout", tmp.name, "-"],
                capture_output=True, timeout=15, check=False,
            )
        if res.returncode != 0:
            logger.debug("pdftotext exit %d: %s", res.returncode, res.stderr[:200])
            return None
        return res.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        logger.debug("pdftotext timeout")
        return None
    except Exception as e:
        logger.debug("pdftotext failed: %s", e)
        return None


# ─── Text → transactions ─────────────────────────────────────────────

# Owner codes that legitimately start a transaction block.
_OWNER_CODES = ("SP", "JT", "DC", "JT/SP", "JT/DC")

# Match the leading line of a transaction block. Layout-mode pdftotext
# preserves columns with whitespace runs, so we pin to "OWNER    Asset…"
# at column ~10. We tolerate a missing owner code (self-owned trades use a
# blank cell that pdftotext usually drops to single spaces).
_TX_LINE_RE = re.compile(
    r"""
    ^\s+
    (?P<owner>SP|JT|DC|JT/SP|JT/DC)?            # optional owner code
    \s*
    (?P<asset>.{8,80}?)                         # asset name (continues on next line)
    \s+
    (?P<tx_type>P|S\s*\(partial\)|S|E)          # transaction code
    \s+
    (?P<tx_date>\d{1,2}/\d{1,2}/\d{2,4})        # transaction date
    \s+
    (?P<notif_date>\d{1,2}/\d{1,2}/\d{2,4})     # notification date
    \s+
    \$?\s*(?P<amt_low>[\d,]+(?:\.\d+)?)         # amount low
    (?:
        \s*-\s*\$?\s*(?P<amt_high>[\d,]+(?:\.\d+)?)
    )?
    (?P<over>\+)?                               # +  → "over" form
    """,
    re.VERBOSE,
)

# Continuation line carrying ticker + asset-type code, e.g.
#   "Stock (GOOGL) [ST]"
# Both the parens and brackets vary in spacing.
_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)")
_ASSET_TYPE_RE = re.compile(r"\[([A-Z]{1,5})\]")

# Lines that mark the start of the transactions section.
_TX_HEADER_RE = re.compile(r"^\s*ID\s+Owner\s+Asset", re.IGNORECASE)

# Description lines start with "D" then variable spacing then ":"
_DESC_RE = re.compile(r"^\s*D\s+:\s*(.*)$")

# Filing-status lines we want to skip when accumulating asset-name continuation.
_NOISE_LINE_RE = re.compile(r"^\s*F\s+S\s+:|^\s*\*|^\s*$")

_AMOUNT_OVER_RE = re.compile(r"\$?\s*([\d,]+)(?:\.\d+)?\s*\+", re.IGNORECASE)

# A line that is ONLY a column-aligned amount value (the high end of a
# bracketed range that wrapped to the next line). E.g. "           $5,000,000"
_AMOUNT_ONLY_RE = re.compile(r"^\s{12,}\$\s*([\d,]+(?:\.\d+)?)\s*\+?\s*$")

# Markers that indicate the transaction table has ended — anything past
# this is the certification block, asset-type code reference, etc.
_TABLE_END_RE = re.compile(
    r"(?:"
    r"^\*\s*For\s+the\s+complete\s+list\s+of\s+asset\s+type"
    r"|I\s+CERTIFY\s+that"
    r"|Digitally\s+Signed"
    r"|^\s*Initial\s+Public\s+Offering\s*\(IPO\)"
    r")",
    re.IGNORECASE,
)


def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _parse_date(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def _normalize_tx_type(raw: str) -> str:
    """P → buy, S/S(partial) → sell, E → exchange."""
    r = re.sub(r"\s+", "", raw or "").upper()
    if r == "P":
        return "buy"
    if r in ("S", "S(PARTIAL)", "S(P)"):
        return "sell"
    if r == "E":
        return "exchange"
    return "other"


def parse_ptr_text(text: str) -> list[dict]:
    """
    Walk the layout-mode text of a House PTR. Yields one dict per parsed
    transaction. Returns [] if the text doesn't look like a PTR (e.g. the
    Clerk returned an interstitial cover page or the PDF is encrypted).
    """
    if not text:
        return []

    lines = text.splitlines()

    # Find the start of the transaction table. Every PTR has a row of
    # column headers ("ID  Owner Asset …"). If we don't find one, bail
    # rather than risk false-positive parses.
    start_idx = None
    for i, ln in enumerate(lines):
        if _TX_HEADER_RE.match(ln):
            start_idx = i + 1
            break
    if start_idx is None:
        return []

    # Walk lines, accumulating multi-line blocks. A new block starts at
    # any line that matches _TX_LINE_RE; intermediate lines belong to the
    # previous block (asset-name continuation, description, amount-high).
    # Stop entirely once we hit a table-end marker.
    blocks: list[list[str]] = []
    current: list[str] = []

    for ln in lines[start_idx:]:
        if _TABLE_END_RE.search(ln):
            break  # certification / footer reached — done
        # Skip page-break repetitions of the column headers
        if _TX_HEADER_RE.match(ln):
            continue
        if _TX_LINE_RE.match(ln):
            if current:
                blocks.append(current)
            current = [ln]
        else:
            if current:
                current.append(ln)
    if current:
        blocks.append(current)

    out: list[dict] = []
    for blk in blocks:
        if not blk:
            continue
        head = blk[0]
        m = _TX_LINE_RE.match(head)
        if not m:
            continue

        owner = (m.group("owner") or "").strip()
        asset_first = (m.group("asset") or "").strip()
        tx_type_raw = (m.group("tx_type") or "").strip()
        tx_date = m.group("tx_date")
        notif_date = m.group("notif_date")
        amt_low = _to_float(m.group("amt_low"))
        amt_high = _to_float(m.group("amt_high"))
        is_over = m.group("over") == "+"

        # Continuation lines come in three flavours and we want to handle
        # them in this exact priority order, EARLY-EXIT once each is found:
        #   1. Amount-high wrap line  (column-aligned $<num>)
        #   2. Description line       ("D : ...")
        #   3. Asset-name continuation (everything else)
        # We also stop accumulating asset-name once we've seen the (TICKER)
        # [TYPE] tags — anything past that on a non-D-line is footer junk.
        full_asset = asset_first
        ticker = None
        asset_type = None
        description_parts: list[str] = []
        seen_ticker = False
        for cont in blk[1:]:
            if not cont.strip():
                continue
            # 1) Standalone amount-high (the right column of a wrapped range)
            if amt_high is None and not is_over:
                am = _AMOUNT_ONLY_RE.match(cont)
                if am:
                    cand = _to_float(am.group(1))
                    if cand and amt_low and cand >= amt_low:
                        amt_high = cand
                        continue
            # Filing-status / footnote noise we always skip
            if _NOISE_LINE_RE.match(cont):
                continue
            # 2) Description ("D     :  ...")
            d = _DESC_RE.match(cont)
            if d:
                description_parts.append(d.group(1).strip())
                continue
            # 3) Asset-name continuation — only until we've captured the ticker.
            #    After that, ignore everything (the next non-D line is usually
            #    page-break header or end-of-table junk).
            if seen_ticker:
                continue
            stripped = cont.strip()
            t = _TICKER_RE.search(stripped)
            a = _ASSET_TYPE_RE.search(stripped)
            if t:
                ticker = t.group(1)
            if a:
                asset_type = a.group(1)
            # Trailing "$<num>" on this line is the amount-high (column-aligned
            # to the right edge by pdftotext layout mode). Strip it before we
            # assign the line to asset-name continuation.
            if amt_high is None and not is_over:
                trailing = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*\+?\s*$", stripped)
                if trailing:
                    cand = _to_float(trailing.group(1))
                    if cand and amt_low and cand >= amt_low:
                        amt_high = cand
                        if "+" in stripped[-3:]:
                            is_over = True
                        stripped = stripped[: trailing.start()].rstrip()
            cleaned = _TICKER_RE.sub("", stripped)
            cleaned = _ASSET_TYPE_RE.sub("", cleaned).strip()
            # Drop common per-page header text that pdftotext interleaves.
            cleaned = re.sub(
                r"\b(Type Date Gains > \$200\?|Notification Date|Transaction Date)\b",
                "", cleaned,
            ).strip()
            if cleaned:
                full_asset = (full_asset + " " + cleaned).strip()
            if ticker:
                seen_ticker = True

        # Some PTRs bury the ticker on the leading line itself
        if not ticker:
            t = _TICKER_RE.search(head)
            if t:
                ticker = t.group(1)
        if not asset_type:
            a = _ASSET_TYPE_RE.search(head)
            if a:
                asset_type = a.group(1)

        # Strip leftover bracket/paren tags that snuck into full_asset
        full_asset = re.sub(r"\s+", " ", _TICKER_RE.sub("", _ASSET_TYPE_RE.sub("", full_asset))).strip()

        side = _normalize_tx_type(tx_type_raw)

        # Sanity: PTRs occasionally have OPTION rows whose asset ticker is the
        # underlying — keep the ticker but stash the option-vs-stock flag.
        out.append({
            "owner": owner,
            "asset_name": full_asset,
            "ticker": ticker,
            "asset_type": asset_type,
            "tx_type_raw": tx_type_raw.strip(),
            "side": side,
            "tx_date_ts": _parse_date(tx_date),
            "tx_date_raw": tx_date,
            "notif_date_ts": _parse_date(notif_date),
            "notif_date_raw": notif_date,
            "amount_low": amt_low,
            "amount_high": amt_high if not is_over else None,
            "amount_over": amt_low if is_over else None,
            "description": " ".join(description_parts).strip() or None,
        })

    return out


def parse_ptr_pdf(url_or_bytes) -> list[dict]:
    """
    Convenience: take either a URL string or raw PDF bytes, return parsed txs.
    Returns [] on any error so callers can downgrade to filing-level data.
    """
    if isinstance(url_or_bytes, str):
        pdf = _fetch_pdf(url_or_bytes)
    elif isinstance(url_or_bytes, (bytes, bytearray)):
        pdf = bytes(url_or_bytes)
    else:
        return []
    if not pdf:
        return []
    text = _pdf_to_text(pdf)
    if not text:
        return []
    return parse_ptr_text(text)


if __name__ == "__main__":
    import json, sys
    logging.basicConfig(level=logging.DEBUG)
    if len(sys.argv) < 2:
        print("usage: ptr_pdf_parser.py <pdf-url-or-path>")
        sys.exit(2)
    arg = sys.argv[1]
    if arg.startswith("http"):
        rows = parse_ptr_pdf(arg)
    else:
        rows = parse_ptr_pdf(Path(arg).read_bytes())
    print(json.dumps(rows, indent=2, default=str))
