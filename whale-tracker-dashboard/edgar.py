"""SEC EDGAR client.

EDGAR is free but requires a polite User-Agent (with contact email) and
caps requests at 10/sec. We add a small semaphore + sleep so we never
trip the rate limit even under burst.

Two access modes are used:
  1. Atom feed of recent filings, grouped by form type.
       https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&output=atom
  2. The per-filing index JSON ("index.json"), which lists every document
     in the filing so we can find the structured XML (Form 4) or the
     primary HTML (8-K) without scraping.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any
from xml.etree import ElementTree as ET

import httpx

log = logging.getLogger("edgar")

EDGAR_BASE = "https://www.sec.gov"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# SEC requires a UA with contact info. Override via env in production.
USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "narve.ai whale tracker contact@narve.ai",
)

# 10 req/sec cap → cap concurrency at 5, ~100ms min spacing.
_sem = asyncio.Semaphore(5)
_last_request_at = 0.0
_request_lock = asyncio.Lock()
_MIN_SPACING_S = 0.12


async def _throttle() -> None:
    global _last_request_at
    async with _request_lock:
        loop = asyncio.get_event_loop()
        now = loop.time()
        wait = _MIN_SPACING_S - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = loop.time()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Host": "www.sec.gov",
        },
        timeout=20.0,
        follow_redirects=True,
    )


async def fetch(url: str) -> str:
    await _throttle()
    async with _sem, _client() as cx:
        r = await cx.get(url)
        r.raise_for_status()
        return r.text


async def fetch_json(url: str) -> Any:
    await _throttle()
    async with _sem, _client() as cx:
        r = await cx.get(url)
        r.raise_for_status()
        return r.json()


async def recent_atom(form_type: str, count: int = 40) -> list[dict]:
    """Return parsed entries from the EDGAR 'getcurrent' Atom feed.

    Each entry has at least: title, summary, link, accession, filer_cik,
    filer_name, filed_at, form_type.
    """
    # `+` becomes %2B but EDGAR also accepts space; use space.
    type_param = form_type.replace("+", " ")
    url = (
        f"{EDGAR_BASE}/cgi-bin/browse-edgar"
        f"?action=getcurrent&type={type_param}&company="
        f"&dateb=&owner=include&count={count}&output=atom"
    )
    body = await fetch(url)
    return _parse_current_atom(body, form_type)


def _parse_current_atom(xml: str, form_type: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.warning("atom parse error for %s: %s", form_type, e)
        return out

    for entry in root.findall("atom:entry", ATOM_NS):
        title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
        summary = (entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").strip()
        updated = (entry.findtext("atom:updated", default="", namespaces=ATOM_NS) or "").strip()
        link_el = entry.find("atom:link", ATOM_NS)
        link = link_el.get("href") if link_el is not None else ""

        # title looks like:  "4 - Some Person (0001234567) (Reporting)"
        # for 8-K it's:      "8-K - ACME CORP (0000098765) (Filer)"
        m = re.match(r"^\s*([^-]+?)\s*-\s*(.*?)\s*\((\d{10})\)\s*\((Reporting|Filer|Filed by)\)", title)
        filer_name = ""
        filer_cik = ""
        if m:
            filer_name = m.group(2).strip()
            filer_cik = m.group(3).strip()

        # Accession is in the link path, like
        # /cgi-bin/browse-edgar?action=getcompany&CIK=...&type=...&...&filenum=...
        # but more reliably the accession appears in the entry id.
        accession = ""
        eid = entry.findtext("atom:id", default="", namespaces=ATOM_NS) or ""
        m2 = re.search(r"accession-number=([\d-]+)", eid)
        if m2:
            accession = m2.group(1)
        else:
            # fall back: pull from link
            m3 = re.search(r"(\d{10}-\d{2}-\d{6})", link)
            if m3:
                accession = m3.group(1)

        out.append({
            "form_type":   form_type,
            "title":       title,
            "summary":     summary,
            "filed_at":    updated,
            "link":        link,
            "accession":   accession,
            "filer_name":  filer_name,
            "filer_cik":   filer_cik,
        })
    return out


def filing_index_url(accession: str) -> str:
    """Return the per-filing index.json URL.

    Accession `0001234567-25-000012` lives at
    /Archives/edgar/data/<cik-no-leading-zeros>/<accession-no-dashes>/index.json
    EDGAR also accepts the form with dashes — but we still need the CIK.
    The simpler stable URL is:
       /Archives/edgar/data/<cik>/<accession_nodash>/<accession>-index.json
    We don't always have the CIK on the entry (we do — from the title parse),
    so fall back to the dash-stripped path with cik passed in.
    """
    raise NotImplementedError("use filing_index_url_for(cik, accession)")


def filing_index_url_for(cik: str, accession: str) -> str:
    cik_int = str(int(cik)) if cik and cik.isdigit() else cik
    nodash = accession.replace("-", "")
    return f"{EDGAR_BASE}/Archives/edgar/data/{cik_int}/{nodash}/{accession}-index.json"


def filing_primary_doc_url(cik: str, accession: str, doc_name: str) -> str:
    cik_int = str(int(cik)) if cik and cik.isdigit() else cik
    nodash = accession.replace("-", "")
    return f"{EDGAR_BASE}/Archives/edgar/data/{cik_int}/{nodash}/{doc_name}"


async def fetch_filing_index(cik: str, accession: str) -> dict | None:
    """Return the parsed index.json for a filing, or None on 404/parse failure."""
    if not cik or not accession:
        return None
    url = filing_index_url_for(cik, accession)
    try:
        return await fetch_json(url)
    except httpx.HTTPStatusError as e:
        log.info("index.json %s for %s: %s", e.response.status_code, accession, url)
        return None
    except Exception as e:
        log.warning("index.json fetch error for %s: %s", accession, e)
        return None


def pick_doc(index_json: dict, suffixes: tuple[str, ...]) -> str | None:
    """Pick the first document in the filing whose name ends with any of `suffixes`."""
    items = (index_json or {}).get("directory", {}).get("item", [])
    for it in items:
        name = it.get("name", "")
        if name.lower().endswith(suffixes):
            return name
    return None
