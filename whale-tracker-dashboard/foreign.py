"""Foreign substantial-shareholder / major-holding notices.

Two adapters, unified schema:
  - ASX (Australia): substantial holder notices from the ASX announcements
    RSS. Watchlist-based (add company codes via POST /api/foreign/asx/companies).
  - JPX / EDINET (Japan): major shareholder reports (Large Shareholding Reports,
    大量保有報告書) via EDINET's public JSON API (no key required).

All rows land in `foreign_holder` with a `source` column ("asx" | "edinet")
so a single UI can render both.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
from typing import Iterable

import httpx

import db

log = logging.getLogger("foreign")

USER_AGENT = "narve.ai whale tracker contact@narve.ai"
_TIMEOUT = 20.0

# ─── ASX ──────────────────────────────────────────────────────────────

ASX_RSS_FMT = os.environ.get(
    "ASX_ANNOUNCEMENTS_URL",
    "https://www.asx.com.au/asx/1/company/{code}/announcements?count=20",
)
_ASX_SEM = asyncio.Semaphore(4)


def list_asx_companies() -> list[dict]:
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT * FROM foreign_company WHERE source = 'asx' ORDER BY code"
        ).fetchall()
    return [dict(r) for r in rows]


def add_asx_company(code: str, name: str | None = None) -> None:
    with db.connect() as cx:
        cx.execute(
            "INSERT OR IGNORE INTO foreign_company (source, code, name, last_pulled_at) "
            "VALUES ('asx', ?, ?, NULL)",
            (code.upper().strip(), name),
        )


def remove_asx_company(code: str) -> None:
    code = code.upper().strip()
    with db.connect() as cx:
        cx.execute("DELETE FROM foreign_holder WHERE source = 'asx' AND issuer_code = ?", (code,))
        cx.execute("DELETE FROM foreign_company WHERE source = 'asx' AND code = ?", (code,))


async def fetch_asx_announcements(code: str) -> list[dict]:
    """Pull ASX company announcements JSON; keep only substantial-holder rows."""
    url = ASX_RSS_FMT.format(code=code.upper().strip())
    async with _ASX_SEM:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT,
                                         headers={"User-Agent": USER_AGENT}) as cx:
                r = await cx.get(url)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.info("asx fetch %s failed: %s", code, e)
            return []
    items = data.get("data") if isinstance(data, dict) else data
    if not items:
        return []
    out: list[dict] = []
    for it in items:
        headline = (it.get("header") or it.get("subject") or "").strip()
        if not _looks_like_substantial(headline):
            continue
        notified = it.get("document_date") or it.get("released_at") or ""
        out.append({
            "source":            "asx",
            "notice_id":         str(it.get("id") or f"asx:{code}:{notified}:{headline[:60]}")[:200],
            "issuer_code":       code.upper().strip(),
            "issuer_name":       (it.get("name") or "").strip(),
            "holder_name":       "",  # ASX headline doesn't structure holder; LLM-extract phase 10
            "notified_at":       notified[:10] if notified else "",
            "percent_ownership": _extract_pct(headline),
            "headline":          headline[:300],
            "url":               (it.get("url") or "").strip(),
        })
    return out


_SUBSTANTIAL_RX = re.compile(
    r"(substantial\s+holder|change\s+in\s+substantial\s+holding|ceasing\s+to\s+be\s+a\s+substantial)",
    re.I,
)
_PCT_RX = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _looks_like_substantial(headline: str) -> bool:
    return bool(_SUBSTANTIAL_RX.search(headline or ""))


def _extract_pct(text: str) -> float | None:
    if not text:
        return None
    m = _PCT_RX.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


async def refresh_asx() -> dict:
    companies = list_asx_companies()
    total = 0
    for c in companies:
        rows = await fetch_asx_announcements(c["code"])
        for r in rows:
            db.upsert_foreign_holder(r)
            total += 1
        with db.connect() as cx:
            cx.execute(
                "UPDATE foreign_company SET last_pulled_at = ? "
                "WHERE source = 'asx' AND code = ?",
                (dt.datetime.utcnow().isoformat(timespec="seconds") + "Z", c["code"]),
            )
    return {"asx_companies": len(companies), "rows": total}


# ─── EDINET (Japan) ───────────────────────────────────────────────────

EDINET_LIST_URL = os.environ.get(
    "EDINET_LIST_URL",
    "https://api.edinet-fsa.go.jp/api/v2/documents.json",
)
EDINET_API_KEY = os.environ.get("EDINET_API_KEY", "").strip()
_EDINET_SEM = asyncio.Semaphore(2)

# Large shareholding report doc type codes (see EDINET spec, dept 100=Securities,
# type 040=Large shareholding report / 大量保有報告書, 041=change/変更報告書).
EDINET_LARGE_HOLDING_TYPES = {"040", "041"}


def is_edinet_configured() -> bool:
    # EDINET API v2 requires a Subscription-Key header for daily-list endpoints.
    return bool(EDINET_API_KEY)


async def fetch_edinet_day(day: str) -> list[dict]:
    """List all documents filed on a given YYYY-MM-DD; filter to large-holdings."""
    if not is_edinet_configured():
        return []
    async with _EDINET_SEM:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, headers={
                "User-Agent": USER_AGENT,
                "Ocp-Apim-Subscription-Key": EDINET_API_KEY,
            }) as cx:
                r = await cx.get(EDINET_LIST_URL, params={"date": day, "type": 2})
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.info("edinet list %s failed: %s", day, e)
            return []
    results = data.get("results") or []
    out: list[dict] = []
    for it in results:
        doc_type = str(it.get("docTypeCode") or "").strip()
        if doc_type not in EDINET_LARGE_HOLDING_TYPES:
            continue
        out.append({
            "source":            "edinet",
            "notice_id":         (it.get("docID") or "")[:200],
            "issuer_code":       (it.get("secCode") or "").strip() or (it.get("edinetCode") or ""),
            "issuer_name":       (it.get("filerName") or it.get("submitDateTime") or "")[:200],
            "holder_name":       (it.get("submitterName") or "").strip()[:200],
            "notified_at":       day,
            "percent_ownership": None,   # not on the list endpoint; LLM-extract in phase 10
            "headline":          f"{it.get('docDescription','')}".strip()[:300],
            "url":               f"https://disclosure.edinet-fsa.go.jp/api/v2/documents/{it.get('docID','')}",
        })
    return out


async def refresh_edinet_recent(days: int = 7) -> dict:
    if not is_edinet_configured():
        return {"configured": False, "rows": 0}
    today = dt.date.today()
    total = 0
    for i in range(int(days)):
        day = (today - dt.timedelta(days=i)).isoformat()
        rows = await fetch_edinet_day(day)
        for r in rows:
            if r.get("notice_id"):
                db.upsert_foreign_holder(r)
                total += 1
    return {"configured": True, "rows": total, "days_scanned": int(days)}


# ─── Read queries ────────────────────────────────────────────────────

def recent_holders(source: str | None = None, days: int = 90, limit: int = 200) -> list[dict]:
    sql = ["SELECT * FROM foreign_holder WHERE notified_at >= date('now', ?)"]
    params: list = [f"-{int(days)} days"]
    if source in ("asx", "edinet"):
        sql.append("AND source = ?")
        params.append(source)
    sql.append("ORDER BY notified_at DESC LIMIT ?")
    params.append(int(limit))
    with db.connect() as cx:
        rows = cx.execute(" ".join(sql), params).fetchall()
    return [dict(r) for r in rows]


def holders_by_issuer(source: str, code: str) -> list[dict]:
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT * FROM foreign_holder WHERE source = ? AND issuer_code = ? "
            "ORDER BY notified_at DESC",
            (source, code.upper().strip()),
        ).fetchall()
    return [dict(r) for r in rows]
