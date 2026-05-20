"""CIK → ticker lookup, plus issuer-name → ticker reverse index.

SEC publishes a free, authoritative CIK→ticker map at
https://www.sec.gov/files/company_tickers.json. We cache it on disk
and refresh once a day. Used to:
  1. enrich 13D/G and 8-K rows with a ticker (we have the filer CIK),
  2. enrich 13F holdings with a ticker via fuzzy issuer-name lookup
     (the INFORMATION TABLE only carries CUSIP + issuer name, not CIK,
     and a free CUSIP→ticker map doesn't exist).

For the name index we normalise both sides: uppercase, drop common
entity-type suffixes (INC, CORP, LLC…), strip punctuation, collapse
whitespace. Big-cap issuers ('APPLE INC' → 'APPLE') hit reliably.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import edgar

log = logging.getLogger("cik_ticker")

CACHE_PATH = Path(__file__).parent / "cik_tickers.json"
TTL_S = 24 * 3600
URL = "https://www.sec.gov/files/company_tickers.json"

_state: dict = {"loaded_at": 0.0, "map": {}, "name_index": {}}

# Entity-type suffixes we strip when normalising issuer names. Order
# matters slightly because longer alternatives must come first.
_SUFFIXES = [
    " INCORPORATED",
    " CORPORATION",
    " LIMITED",
    " HOLDINGS",
    " COMPANY",
    " GROUP",
    " INC.",
    " CORP.",
    " LTD.",
    " LLC.",
    " CO.",
    " PLC.",
    " INC",
    " CORP",
    " LTD",
    " LLC",
    " CO",
    " PLC",
    " S.A.",
    " S.A",
    " N.V.",
    " NV",
    " A.G.",
    " AG",
    " A.B.",
    " AB",
    " ASA",
    " SE",
]
_PUNCT_RX = re.compile(r"[^A-Z0-9 ]+")
_WS_RX    = re.compile(r"\s+")


def _normalise_issuer(name: str) -> str:
    if not name:
        return ""
    s = name.upper().strip()
    s = _PUNCT_RX.sub(" ", s)
    s = _WS_RX.sub(" ", s).strip()
    # Strip suffixes iteratively — some filings stack them ("FOO INC HOLDINGS").
    changed = True
    while changed:
        changed = False
        for sfx in _SUFFIXES:
            if s.endswith(sfx):
                s = s[: -len(sfx)].strip()
                changed = True
                break
            sfx_no_punct = sfx.replace(".", "").rstrip()
            if sfx_no_punct and s.endswith(sfx_no_punct):
                s = s[: -len(sfx_no_punct)].strip()
                changed = True
                break
    return s


def _build_name_index(m: dict[str, dict]) -> dict[str, str]:
    """normalised issuer name → ticker. Ambiguous names get dropped."""
    counts: dict[str, set[str]] = {}
    for entry in m.values():
        ticker = (entry.get("ticker") or "").upper()
        name = entry.get("name") or ""
        if not ticker or not name:
            continue
        key = _normalise_issuer(name)
        if not key:
            continue
        counts.setdefault(key, set()).add(ticker)
    return {k: next(iter(v)) for k, v in counts.items() if len(v) == 1}


async def ensure_loaded() -> int:
    """Make sure the map is loaded. Returns entry count."""
    if _state["map"] and (time.time() - _state["loaded_at"]) < TTL_S:
        return len(_state["map"])

    if CACHE_PATH.exists() and (time.time() - CACHE_PATH.stat().st_mtime) < TTL_S:
        try:
            _state["map"] = json.loads(CACHE_PATH.read_text())
            _state["loaded_at"] = time.time()
            _state["name_index"] = _build_name_index(_state["map"])
            return len(_state["map"])
        except Exception as e:
            log.warning("cik_tickers cache parse failed (%s) — refetching", e)

    try:
        data = await edgar.fetch_json(URL)
    except Exception as e:
        log.warning("cik_tickers fetch failed: %s", e)
        return len(_state["map"])

    # data is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    m: dict[str, dict] = {}
    for v in data.values():
        cik_int = str(v.get("cik_str", ""))
        if not cik_int:
            continue
        entry = {"ticker": (v.get("ticker") or "").upper(), "name": v.get("title", "")}
        m[cik_int] = entry
        m[cik_int.zfill(10)] = entry

    _state["map"] = m
    _state["loaded_at"] = time.time()
    _state["name_index"] = _build_name_index(m)
    try:
        CACHE_PATH.write_text(json.dumps(m))
    except Exception as e:
        log.info("cik_tickers cache write skipped: %s", e)
    log.info("cik_tickers loaded: %d cik entries, %d unambiguous names",
             len(m), len(_state["name_index"]))
    return len(m)


def lookup(cik: str) -> dict | None:
    if not cik:
        return None
    m = _state["map"]
    if not m:
        return None
    if cik in m:
        return m[cik]
    if cik.isdigit():
        s = str(int(cik))
        return m.get(s) or m.get(s.zfill(10))
    return None


def lookup_ticker(cik: str) -> str | None:
    e = lookup(cik)
    return e["ticker"] if e and e.get("ticker") else None


def lookup_name(cik: str) -> str | None:
    e = lookup(cik)
    return e["name"] if e and e.get("name") else None


def resolve_ticker_from_name(issuer_name: str) -> str | None:
    """Best-effort ticker lookup from a 13F-style issuer name."""
    if not issuer_name or not _state["name_index"]:
        return None
    key = _normalise_issuer(issuer_name)
    return _state["name_index"].get(key)


def name_index_size() -> int:
    return len(_state["name_index"])

