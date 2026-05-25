#!/usr/bin/env python3
"""
SEC enforcement archive ingester.

Pulls SEC Litigation Releases from the public index at
https://www.sec.gov/enforcement-litigation/litigation-releases (paginated,
100 releases/page) and stores them in a new `enforcement_cases` table.

Two-pass design (mirrors the Form 4 + congress-PTR pattern):

  Pass 1 (cheap, ~0.5s/page):
    Walk the paginated index, extract per release:
      - case_id (LR-26540)
      - filed_date (publish date)
      - defendants[]  (parsed from `release-view__respondents` div)
      - summary (one-line from index)
      - source_url
    Insert with is_insider_related=NULL — unknown without body text.

  Pass 2 (slower, only on unenriched cases):
    Fetch the detail page, extract the body, flag is_insider_related=1
    if it contains any insider-trading keywords. Cap to N/pass to avoid
    hammering sec.gov.

Defendant→actor matching lives in `enforcement_match.py` to keep this
module focused on ingest.

SEC compliance:
  - Honors the same SEC_USER_AGENT env var as edgar_form4.py (mandatory).
  - Stays well under SEC's 10 req/sec ceiling (RATE_PAUSE = 0.2s).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "insider_events.db"  # share DB
INDEX_URL = (
    "https://www.sec.gov/enforcement-litigation/litigation-releases"
    "?items_per_page=200&page={page}"
)
DETAIL_URL_BASE = "https://www.sec.gov"
HTTP_TIMEOUT = 25.0
RATE_PAUSE = 0.20
MAX_PAGES_AT_BOOT = 30        # ~3,000 releases ≈ ~5 years back
MAX_PAGES_STEADY = 2          # ~200 newest releases — catches anything new
MAX_DETAIL_FETCHES_PER_PASS = 40

# Keywords that flag a release as insider-trading-related. Tuned to be
# permissive enough to catch tipper/tippee variants, strict enough to
# avoid generic disclosure-fraud cases.
INSIDER_KEYWORDS = (
    "insider trading",
    "material non-public information",
    "material nonpublic information",
    "MNPI",
    "tippee", "tipper", "tipped",
    "while in possession of material",
    "misappropriation theory",
    "Section 10(b)",  # the workhorse insider-trading statute
    "Rule 10b-5",
)
_INSIDER_RE = re.compile("|".join(re.escape(k) for k in INSIDER_KEYWORDS), re.IGNORECASE)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS enforcement_cases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    regulator           TEXT NOT NULL,         -- 'SEC', future: 'FCA', 'BaFin', etc.
    jurisdiction        TEXT NOT NULL,         -- 'US', future: 'UK', 'DE', ...
    case_id             TEXT NOT NULL,         -- e.g. 'LR-26540'
    filed_date          INTEGER,               -- unix; release publish date
    title               TEXT,                  -- defendant string from index
    defendants_json     TEXT,                  -- JSON array of normalized names
    summary             TEXT,                  -- snippet (index or detail body)
    source_url          TEXT,
    is_insider_related  INTEGER,               -- NULL=unknown, 0=no, 1=yes
    enriched            INTEGER NOT NULL DEFAULT 0,
    extra_json          TEXT,
    ingested_at         INTEGER NOT NULL,
    UNIQUE(regulator, case_id)
);
CREATE INDEX IF NOT EXISTS idx_enf_cases_filed
    ON enforcement_cases(filed_date DESC);
CREATE INDEX IF NOT EXISTS idx_enf_cases_insider
    ON enforcement_cases(is_insider_related);
CREATE INDEX IF NOT EXISTS idx_enf_cases_enriched
    ON enforcement_cases(enriched);

CREATE TABLE IF NOT EXISTS enforcement_actor_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    enforcement_id  INTEGER NOT NULL,
    actor_id        TEXT NOT NULL,
    defendant_name  TEXT NOT NULL,
    match_confidence REAL NOT NULL,            -- 0-1
    match_method    TEXT NOT NULL,             -- 'fuzzy_name', 'exact_normalized', 'manual'
    created_at      INTEGER NOT NULL,
    UNIQUE(enforcement_id, actor_id),
    FOREIGN KEY(enforcement_id) REFERENCES enforcement_cases(id)
);
CREATE INDEX IF NOT EXISTS idx_enf_links_actor
    ON enforcement_actor_links(actor_id);
CREATE INDEX IF NOT EXISTS idx_enf_links_enf
    ON enforcement_actor_links(enforcement_id);
"""


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    import insider_events
    insider_events.init_db()
    with _conn() as c:
        c.executescript(_SCHEMA)


def is_available() -> bool:
    """Whether SEC_USER_AGENT is set so we can identify ourselves to sec.gov."""
    return bool(os.environ.get("SEC_USER_AGENT"))


def _http_client() -> httpx.Client:
    ua = os.environ.get("SEC_USER_AGENT") or "narve-insider (contact via narve.ai)"
    return httpx.Client(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
        follow_redirects=True,
    )


# ─── Index parsing ───────────────────────────────────────────────────

# Each release on the index page contains:
#   <div class='release-view__respondents'><a href='/enforcement-litigation/...lr-26540'>Defendant Name(s)</a></div>
#   <span class='view-table_subfield_value'>LR-26540</span>
#   <time datetime="2026-04-24T20:43:42Z" class="datetime">April 24, 2026</time>
# Order isn't guaranteed in the markup, so we anchor on the respondents block
# and look forward/back for the LR + date.
_BLOCK_RE = re.compile(
    r"release-view__respondents'><a href='([^']+)'>(.+?)</a>"
    r".{0,3000}?LR-(\d+)"
    r".{0,3000}?datetime=\"([^\"]+)\"",
    re.DOTALL,
)
# Date alt — sometimes datetime appears BEFORE the respondents block on a row.
_BLOCK_RE_ALT = re.compile(
    r"datetime=\"([^\"]+)\".{0,3000}?"
    r"release-view__respondents'><a href='([^']+)'>(.+?)</a>"
    r".{0,3000}?LR-(\d+)",
    re.DOTALL,
)


def _parse_iso_to_unix(iso: str) -> int | None:
    if not iso:
        return None
    try:
        # SEC uses "2026-04-24T20:43:42Z"
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _normalize_defendant(raw: str) -> list[str]:
    """
    Turn '<a>Foo Corp., John Smith, and Jane Doe</a>' into ['Foo Corp.',
    'John Smith', 'Jane Doe']. Drops noise, keeps the verbatim casing for
    display; matching does its own normalization downstream.
    """
    s = html.unescape(raw or "").strip()
    if not s:
        return []
    # SEC uses ", and " or ", " or "; " as separators
    s = re.sub(r"\s+", " ", s)
    parts = re.split(r",?\s+and\s+|,\s*|;\s*", s)
    out = []
    for p in parts:
        p = p.strip(" .,;")
        # Drop obvious tail-junk
        if not p or p.lower() in ("et al", "et al."):
            continue
        # Drop very short fragments (probably regex artefacts)
        if len(p) < 3:
            continue
        out.append(p)
    return out


def fetch_index_page(client: httpx.Client, page: int) -> list[dict]:
    """One page of the LR index → list of {case_id, filed_date, title, defendants, source_url, summary}."""
    url = INDEX_URL.format(page=page)
    try:
        r = client.get(url)
        if r.status_code != 200:
            logger.warning("LR index page %d: HTTP %d", page, r.status_code)
            return []
        txt = r.text
    except Exception as e:
        logger.warning("LR index fetch %d failed: %s", page, e)
        return []

    rows: dict[str, dict] = {}
    for m in _BLOCK_RE.finditer(txt):
        href, title_html, lr_num, dt_iso = m.group(1), m.group(2), m.group(3), m.group(4)
        case_id = f"LR-{lr_num}"
        if case_id in rows:
            continue
        defendants = _normalize_defendant(title_html)
        rows[case_id] = {
            "case_id": case_id,
            "filed_date": _parse_iso_to_unix(dt_iso),
            "title": " ".join(d for d in defendants) or html.unescape(title_html).strip(),
            "defendants": defendants,
            "source_url": DETAIL_URL_BASE + href if href.startswith("/") else href,
            "summary": None,  # populated by Pass 2
        }
    # Some pages flip the order of date/respondents — try the alt regex too
    for m in _BLOCK_RE_ALT.finditer(txt):
        dt_iso, href, title_html, lr_num = m.group(1), m.group(2), m.group(3), m.group(4)
        case_id = f"LR-{lr_num}"
        if case_id in rows:
            continue
        defendants = _normalize_defendant(title_html)
        rows[case_id] = {
            "case_id": case_id,
            "filed_date": _parse_iso_to_unix(dt_iso),
            "title": " ".join(defendants) or html.unescape(title_html).strip(),
            "defendants": defendants,
            "source_url": DETAIL_URL_BASE + href if href.startswith("/") else href,
            "summary": None,
        }
    return list(rows.values())


# ─── Pass 1: index ingest ────────────────────────────────────────────

def _upsert_case(c: sqlite3.Connection, row: dict) -> bool:
    now = int(time.time())
    cur = c.execute(
        """
        INSERT INTO enforcement_cases (
            regulator, jurisdiction, case_id, filed_date, title,
            defendants_json, summary, source_url, is_insider_related,
            enriched, extra_json, ingested_at
        ) VALUES ('SEC','US',?,?,?,?,?,?,NULL,0,NULL,?)
        ON CONFLICT(regulator, case_id) DO UPDATE SET
            filed_date=COALESCE(excluded.filed_date, filed_date),
            title=excluded.title,
            defendants_json=excluded.defendants_json,
            source_url=excluded.source_url
        """,
        (
            row["case_id"], row.get("filed_date"), row.get("title"),
            json.dumps(row.get("defendants") or []),
            row.get("summary"),
            row.get("source_url"),
            now,
        ),
    )
    return cur.rowcount > 0


def run_index_ingest(*, max_pages: int = MAX_PAGES_STEADY) -> dict:
    """Pass 1: pull the most recent N pages of the LR index and upsert."""
    if not is_available():
        return {"ok": False, "reason": "SEC_USER_AGENT not set"}
    init_db()

    pages_seen = total_seen = inserted = 0
    with _http_client() as client:
        for page in range(max_pages):
            rows = fetch_index_page(client, page)
            time.sleep(RATE_PAUSE)
            if not rows:
                break
            pages_seen += 1
            total_seen += len(rows)
            with _conn() as c:
                for row in rows:
                    try:
                        if _upsert_case(c, row):
                            inserted += 1
                    except Exception as e:
                        logger.debug("upsert failed for %s: %s", row.get("case_id"), e)
    return {
        "ok": True,
        "pages_seen": pages_seen,
        "rows_seen": total_seen,
        "inserted": inserted,
    }


# ─── Pass 2: detail enrichment + insider classification ──────────────

def _extract_body(detail_html: str) -> str | None:
    """Pull the prose body from a release detail page. SEC pages always
    open with 'On <Month> <day>, <year>'; we anchor on that."""
    m = re.search(
        r"(On (?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d+,\s+\d{4}.{200,8000})(?=<aside|<footer|</main)",
        detail_html, re.DOTALL,
    )
    if not m:
        return None
    body = re.sub(r"<[^>]+>", " ", m.group(1))
    body = re.sub(r"\s+", " ", html.unescape(body)).strip()
    return body[:6000] or None


def _is_insider_related(body: str | None, title: str | None) -> bool:
    blob = " ".join(filter(None, [body or "", title or ""]))
    return bool(_INSIDER_RE.search(blob))


def _cases_needing_enrichment(limit: int) -> list[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT id, case_id, source_url, title
            FROM enforcement_cases
            WHERE regulator = 'SEC'
              AND enriched = 0
              AND source_url IS NOT NULL
            ORDER BY COALESCE(filed_date, ingested_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _mark_enriched(case_db_id: int, body: str | None, is_insider: bool) -> None:
    summary = (body or "")[:600] or None
    with _conn() as c:
        c.execute(
            """
            UPDATE enforcement_cases
            SET enriched = 1,
                summary = COALESCE(?, summary),
                is_insider_related = ?
            WHERE id = ?
            """,
            (summary, 1 if is_insider else 0, case_db_id),
        )


def run_detail_enrich(*, max_cases: int = MAX_DETAIL_FETCHES_PER_PASS) -> dict:
    """Pass 2: fetch detail pages for unenriched cases, flag insider-related."""
    if not is_available():
        return {"ok": False, "reason": "SEC_USER_AGENT not set"}
    cases = _cases_needing_enrichment(max_cases)
    if not cases:
        return {"ok": True, "cases_seen": 0, "fetched": 0, "flagged_insider": 0}

    fetched = flagged = errors = 0
    with _http_client() as client:
        for c in cases:
            url = c["source_url"]
            try:
                r = client.get(url)
                if r.status_code != 200:
                    errors += 1
                    continue
                fetched += 1
                body = _extract_body(r.text)
                is_insider = _is_insider_related(body, c.get("title"))
                _mark_enriched(c["id"], body, is_insider)
                if is_insider:
                    flagged += 1
            except Exception as e:
                logger.debug("detail fetch %s failed: %s", c.get("case_id"), e)
                errors += 1
            time.sleep(RATE_PAUSE)
    return {
        "ok": True,
        "cases_seen": len(cases),
        "fetched": fetched,
        "flagged_insider": flagged,
        "errors": errors,
    }


# ─── Reads ────────────────────────────────────────────────────────────

def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    if d.get("defendants_json"):
        try:
            d["defendants"] = json.loads(d["defendants_json"])
        except Exception:
            d["defendants"] = []
    else:
        d["defendants"] = []
    d.pop("defendants_json", None)
    return d


def recent_cases(
    *,
    insider_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    init_db()
    sql = "SELECT * FROM enforcement_cases WHERE 1=1"
    params: list = []
    if insider_only:
        sql += " AND is_insider_related = 1"
    sql += " ORDER BY filed_date DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [_row(r) for r in rows]


def stats_summary() -> dict:
    init_db()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM enforcement_cases").fetchone()["n"]
        insider = c.execute(
            "SELECT COUNT(*) AS n FROM enforcement_cases WHERE is_insider_related = 1"
        ).fetchone()["n"]
        enriched = c.execute(
            "SELECT COUNT(*) AS n FROM enforcement_cases WHERE enriched = 1"
        ).fetchone()["n"]
        last = c.execute(
            "SELECT MAX(filed_date) AS t FROM enforcement_cases"
        ).fetchone()["t"]
    return {
        "total_cases": total,
        "insider_flagged": insider,
        "enriched": enriched,
        "newest_filed_at": last,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Index pass:", json.dumps(run_index_ingest(max_pages=2), indent=2))
    print("Detail pass:", json.dumps(run_detail_enrich(max_cases=10), indent=2))
    print("Summary:", json.dumps(stats_summary(), indent=2))
