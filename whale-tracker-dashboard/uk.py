"""UK Companies House — Persons with Significant Control (PSC).

PSCs are the UK analog to US 13D filings: anyone with > 25% ownership
of a UK company must register. This is *the* free foreign substantial-
shareholder feed.

Companies House API: https://developer.company-information.service.gov.uk/
Free tier: 600 requests / 5-minute window, API key required (signup is
free, no payment). Set:
    UK_COMPANIES_HOUSE_API_KEY=...

The API is per-company — there's no "recent PSC filings" firehose, so
this module is watchlist-based: maintain a list of UK company numbers
(via /api/uk/companies POST) and we pull PSC for each on a slow cadence.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import datetime as dt

import httpx

import db

log = logging.getLogger("uk")

API_KEY  = os.environ.get("UK_COMPANIES_HOUSE_API_KEY", "").strip()
BASE_URL = os.environ.get("UK_COMPANIES_HOUSE_BASE_URL",
                          "https://api.company-information.service.gov.uk").rstrip("/")
USER_AGENT = "narve.ai whale tracker contact@narve.ai"
_TIMEOUT = 20.0
_sem = asyncio.Semaphore(2)


def is_configured() -> bool:
    return bool(API_KEY)


def _client() -> httpx.AsyncClient:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if API_KEY:
        # Companies House uses Basic auth with the key as the username.
        token = base64.b64encode(f"{API_KEY}:".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return httpx.AsyncClient(headers=headers, timeout=_TIMEOUT, follow_redirects=True)


def _now() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ─── Company watchlist CRUD ──────────────────────────────────────────

def list_companies() -> list[dict]:
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT company_number, name, last_pulled_at FROM uk_company ORDER BY company_number"
        ).fetchall()
    return [dict(r) for r in rows]


def add_company(company_number: str, name: str | None = None) -> None:
    cn = company_number.strip().upper()
    if not cn:
        return
    with db.connect() as cx:
        cx.execute(
            "INSERT OR IGNORE INTO uk_company (company_number, name, last_pulled_at) "
            "VALUES (?, ?, NULL)",
            (cn, name),
        )


def remove_company(company_number: str) -> None:
    cn = company_number.strip().upper()
    with db.connect() as cx:
        cx.execute("DELETE FROM uk_psc WHERE company_number = ?", (cn,))
        cx.execute("DELETE FROM uk_company WHERE company_number = ?", (cn,))


# ─── PSC fetch ───────────────────────────────────────────────────────

async def fetch_psc(company_number: str) -> list[dict]:
    """Fetch PSC list for a UK company. Empty list on failure."""
    if not is_configured():
        return []
    cn = company_number.strip().upper()
    url = f"{BASE_URL}/company/{cn}/persons-with-significant-control"
    async with _sem:
        try:
            async with _client() as cx:
                r = await cx.get(url, params={"items_per_page": 100})
                if r.status_code == 404:
                    return []
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.info("uk psc fetch %s failed: %s", cn, e)
            return []
    items = data.get("items") or []
    return [_normalise_psc(cn, it) for it in items if isinstance(it, dict)]


def _normalise_psc(company_number: str, it: dict) -> dict:
    return {
        "company_number":   company_number,
        "psc_id":           str(it.get("links", {}).get("self") or it.get("etag") or it.get("name") or "")[:200],
        "name":             it.get("name") or it.get("name_elements", {}).get("forename", ""),
        "kind":             it.get("kind") or "",
        "notified_at":      it.get("notified_on") or "",
        "nature_of_control": ",".join(it.get("natures_of_control") or [])[:1000],
        "nationality":      (it.get("nationality") or "")[:100],
    }


async def refresh_company(company_number: str) -> int:
    """Pull PSC for one company, upsert into DB. Returns rows written."""
    pscs = await fetch_psc(company_number)
    if not pscs:
        return 0
    with db.connect() as cx:
        for p in pscs:
            if not p.get("psc_id"):
                continue
            cx.execute(
                """
                INSERT OR REPLACE INTO uk_psc (
                    company_number, psc_id, name, kind, notified_at,
                    nature_of_control, nationality
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (p["company_number"], p["psc_id"], p["name"], p["kind"],
                 p["notified_at"], p["nature_of_control"], p["nationality"]),
            )
        cx.execute(
            "UPDATE uk_company SET last_pulled_at = ? WHERE company_number = ?",
            (_now(), company_number),
        )
    return len(pscs)


async def refresh_all() -> dict:
    if not is_configured():
        return {"refreshed_companies": 0, "psc_rows": 0,
                "note": "UK_COMPANIES_HOUSE_API_KEY not set"}
    companies = list_companies()
    total = 0
    for c in companies:
        n = await refresh_company(c["company_number"])
        total += n
    return {"refreshed_companies": len(companies), "psc_rows": total}


# ─── Read queries ────────────────────────────────────────────────────

def recent_psc(days: int = 90, limit: int = 200) -> list[dict]:
    with db.connect() as cx:
        rows = cx.execute(
            """
            SELECT p.*, c.name AS company_name FROM uk_psc p
            LEFT JOIN uk_company c ON c.company_number = p.company_number
            WHERE p.notified_at >= date('now', ?)
            ORDER BY p.notified_at DESC
            LIMIT ?
            """,
            (f"-{int(days)} days", int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def psc_by_company(company_number: str) -> list[dict]:
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT * FROM uk_psc WHERE company_number = ? ORDER BY notified_at DESC",
            (company_number.strip().upper(),),
        ).fetchall()
    return [dict(r) for r in rows]
