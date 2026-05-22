#!/usr/bin/env python3
"""
Defendant → actor matcher.

Walks `enforcement_cases` defendants and tries to match each name against
existing `insider_events.actor_id` rows. The match produces an
`enforcement_actor_links` row with confidence + method.

Matching tactics, in priority order:

  1. exact_normalized — defendant's last+first (slug form) literally equals
     a known actor_id like 'house:pelosi-nancy' or matches an actor_label
     exactly after lowercase + punctuation strip. confidence=1.0.

  2. fuzzy_name — difflib SequenceMatcher ratio ≥ FUZZY_THRESHOLD between
     normalized defendant and normalized actor_label. confidence=ratio.
     We also require the LAST NAME to appear verbatim in the actor's
     name to suppress "John Smith ≈ John Smyth" → "John Doe" drift.

  3. (manual) — left for future UI; method='manual', confidence=1.0.

The "still trading" view uses these links plus a freshness check on the
actor's most recent insider_event.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "insider_events.db"
FUZZY_THRESHOLD = 0.86           # tuned to be conservative; tighter is better
MAX_DEFENDANTS_PER_CASE = 10     # safety: malformed parses can produce dozens
RECENT_TRADER_WINDOW_DAYS = 540  # actor counts as "still trading" if event in this window
MAX_CASES_PER_PASS = 600


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


# ─── Name normalization ─────────────────────────────────────────────

_HONORIFICS = re.compile(
    r"^(hon\.?|mr\.?|mrs\.?|ms\.?|dr\.?|sen\.?|rep\.?|sir|lord|lady)\s+",
    re.IGNORECASE,
)
_SUFFIXES = re.compile(r"\s+(jr\.?|sr\.?|ii|iii|iv|v|esq\.?)$", re.IGNORECASE)
_NONALPHA = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    n = name.strip()
    n = _HONORIFICS.sub("", n)
    n = _SUFFIXES.sub("", n)
    n = n.lower()
    n = _NONALPHA.sub(" ", n)
    n = _WS.sub(" ", n).strip()
    return n


def _last_name(name_norm: str) -> str:
    """Roughly: last whitespace-separated token from a normalized name."""
    parts = name_norm.split()
    return parts[-1] if parts else ""


def _slugify_lastfirst(name_norm: str) -> str:
    """Mirror congress_ptr._actor_id_from_name's slug form: 'last-first'."""
    parts = name_norm.split()
    if len(parts) >= 2:
        return f"{parts[-1]}-{parts[0]}"
    return parts[0] if parts else ""


# ─── Actor index (cache one snapshot per pass) ──────────────────────

def _build_actor_index() -> tuple[dict[str, list[dict]], list[dict]]:
    """
    Returns (by_lastname_index, all_actors).
      all_actors: list of {actor_id, label_norm, last_name_norm, last_event_ts}
      by_lastname_index: {last_name_norm: [actor records]}
    """
    init_db()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT actor_id, MAX(actor_label) AS actor_label,
                   MAX(actor_role) AS actor_role,
                   MAX(COALESCE(ts_filed, ts_executed, created_at)) AS last_event_ts
            FROM insider_events
            WHERE actor_id IS NOT NULL
            GROUP BY actor_id
            """
        ).fetchall()
    all_actors: list[dict] = []
    by_last: dict[str, list[dict]] = {}
    for r in rows:
        label = r["actor_label"] or r["actor_id"]
        norm = _normalize_name(label)
        ln = _last_name(norm)
        if not ln:
            continue
        rec = {
            "actor_id": r["actor_id"],
            "actor_label": label,
            "actor_role": r["actor_role"],
            "label_norm": norm,
            "last_name_norm": ln,
            "last_event_ts": r["last_event_ts"],
        }
        all_actors.append(rec)
        by_last.setdefault(ln, []).append(rec)
    return by_last, all_actors


# ─── Matching ────────────────────────────────────────────────────────

def _try_exact(defendant_norm: str, by_last: dict) -> dict | None:
    """Slug form match: 'pelosi-nancy' == suffix of 'house:pelosi-nancy'."""
    slug = _slugify_lastfirst(defendant_norm)
    if not slug:
        return None
    ln = _last_name(defendant_norm)
    candidates = by_last.get(ln, [])
    # Compare against each candidate's actor_id slug + label_norm
    for c in candidates:
        # Slug-style actor_ids end with the slug (after any 'house:' / 'senate:')
        aid = c["actor_id"]
        aid_slug = aid.split(":", 1)[-1]
        if aid_slug == slug:
            return {**c, "match_method": "exact_normalized", "match_confidence": 1.0}
        # OR label_norm equals defendant_norm (Form 4 actors don't use slugs)
        if c["label_norm"] == defendant_norm:
            return {**c, "match_method": "exact_normalized", "match_confidence": 1.0}
    return None


def _try_fuzzy(defendant_norm: str, by_last: dict) -> dict | None:
    """Last-name-bucket scan with difflib ratio."""
    ln = _last_name(defendant_norm)
    if not ln or len(ln) < 3:
        return None
    candidates = by_last.get(ln, [])
    if not candidates:
        return None
    best: dict | None = None
    best_ratio = 0.0
    for c in candidates:
        ratio = SequenceMatcher(None, defendant_norm, c["label_norm"]).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = c
    if best and best_ratio >= FUZZY_THRESHOLD:
        return {**best, "match_method": "fuzzy_name", "match_confidence": round(best_ratio, 3)}
    return None


def match_defendant(defendant: str, by_last: dict) -> dict | None:
    """Run the priority chain on one defendant string."""
    norm = _normalize_name(defendant)
    if not norm or len(norm) < 4:
        return None
    return _try_exact(norm, by_last) or _try_fuzzy(norm, by_last)


# ─── Pass orchestrator ──────────────────────────────────────────────

def init_db() -> None:
    import sec_litigation
    sec_litigation.init_db()
    # Match-state table — separate from sec_litigation's schema because
    # this is matcher state, not enforcement-archive state.
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS enforcement_match_state (
                enforcement_id   INTEGER PRIMARY KEY,
                actors_snapshot  INTEGER NOT NULL,
                attempted_at     INTEGER NOT NULL,
                FOREIGN KEY(enforcement_id) REFERENCES enforcement_cases(id)
            );
        """)


def _unmatched_cases(limit: int) -> list[dict]:
    """Cases we haven't yet tried to match defendants on."""
    init_db()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT e.id, e.case_id, e.title, e.defendants_json, e.filed_date
            FROM enforcement_cases e
            LEFT JOIN enforcement_match_state s ON s.enforcement_id = e.id
            WHERE s.enforcement_id IS NULL
            ORDER BY COALESCE(e.filed_date, e.ingested_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            defendants = json.loads(r["defendants_json"] or "[]")
        except Exception:
            defendants = []
        out.append({
            "id": r["id"], "case_id": r["case_id"], "title": r["title"],
            "defendants": defendants[:MAX_DEFENDANTS_PER_CASE],
            "filed_date": r["filed_date"],
        })
    return out


def _record_attempt(case_id: int, actor_count: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO enforcement_match_state "
            "(enforcement_id, actors_snapshot, attempted_at) VALUES (?, ?, ?)",
            (case_id, actor_count, int(time.time())),
        )


def run_match_pass(*, max_cases: int = MAX_CASES_PER_PASS) -> dict:
    """Walk unmatched cases, try to link each defendant to an actor."""
    init_db()
    by_last, all_actors = _build_actor_index()
    if not all_actors:
        return {"ok": True, "cases_seen": 0, "links_created": 0,
                "reason": "no_actors_yet"}

    cases = _unmatched_cases(max_cases)
    if not cases:
        return {"ok": True, "cases_seen": 0, "links_created": 0}

    links_created = defendants_seen = 0
    now = int(time.time())
    with _conn() as c:
        for case in cases:
            defendants_seen += len(case["defendants"])
            for d_name in case["defendants"]:
                m = match_defendant(d_name, by_last)
                if not m:
                    continue
                cur = c.execute(
                    """
                    INSERT OR IGNORE INTO enforcement_actor_links
                    (enforcement_id, actor_id, defendant_name,
                     match_confidence, match_method, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (case["id"], m["actor_id"], d_name,
                     m["match_confidence"], m["match_method"], now),
                )
                if cur.rowcount > 0:
                    links_created += 1
            _record_attempt(case["id"], len(all_actors))
    return {
        "ok": True,
        "cases_seen": len(cases),
        "defendants_seen": defendants_seen,
        "links_created": links_created,
        "actors_in_index": len(all_actors),
    }


def reconsider_all() -> dict:
    """Wipe match_state and rerun — useful after a fresh actor ingest."""
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM enforcement_match_state")
    return run_match_pass(max_cases=10000)


# ─── Reads ────────────────────────────────────────────────────────────

def active_defendants(*, since_days: int = RECENT_TRADER_WINDOW_DAYS,
                      limit: int = 100) -> list[dict]:
    """
    Defendants in enforcement_cases whose linked actor has filed an event
    in the last N days. The headline "people who got busted and are STILL
    trading" view.

    Returns a row per (case, actor) pair joined to the actor's most recent
    activity + their leakage score (if computed).
    """
    init_db()
    cutoff = int(time.time()) - since_days * 86400
    with _conn() as c:
        rows = c.execute(
            """
            SELECT
                e.id              AS enforcement_id,
                e.regulator,
                e.case_id,
                e.title           AS case_title,
                e.filed_date      AS case_filed_date,
                e.is_insider_related,
                e.source_url,
                l.defendant_name,
                l.actor_id,
                l.match_confidence,
                l.match_method,
                ev.actor_label,
                ev.actor_role,
                ev.last_event_ts,
                ev.event_count,
                s.leakage_score,
                s.leakage_percentile,
                s.cross_venue_matches
            FROM enforcement_cases e
            JOIN enforcement_actor_links l ON l.enforcement_id = e.id
            JOIN (
                SELECT actor_id,
                       MAX(actor_label) AS actor_label,
                       MAX(actor_role)  AS actor_role,
                       MAX(COALESCE(ts_filed, ts_executed, created_at)) AS last_event_ts,
                       COUNT(*) AS event_count
                FROM insider_events
                WHERE actor_id IS NOT NULL
                GROUP BY actor_id
            ) ev ON ev.actor_id = l.actor_id
            LEFT JOIN actor_scores s ON s.actor_id = l.actor_id
            WHERE ev.last_event_ts >= ?
            ORDER BY e.filed_date DESC, l.match_confidence DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def enforcement_for_actor(actor_id: str) -> list[dict]:
    """All enforcement cases linked to one actor (for the profile page)."""
    init_db()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT e.*, l.defendant_name, l.match_confidence, l.match_method
            FROM enforcement_cases e
            JOIN enforcement_actor_links l ON l.enforcement_id = e.id
            WHERE l.actor_id = ?
            ORDER BY e.filed_date DESC
            """,
            (actor_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("defendants_json"):
            try:
                d["defendants"] = json.loads(d["defendants_json"])
            except Exception:
                d["defendants"] = []
        d.pop("defendants_json", None)
        out.append(d)
    return out


def match_summary() -> dict:
    init_db()
    with _conn() as c:
        total_cases = c.execute("SELECT COUNT(*) AS n FROM enforcement_cases").fetchone()["n"]
        attempted = c.execute("SELECT COUNT(*) AS n FROM enforcement_match_state").fetchone()["n"]
        links = c.execute("SELECT COUNT(*) AS n FROM enforcement_actor_links").fetchone()["n"]
        unique_actors = c.execute(
            "SELECT COUNT(DISTINCT actor_id) AS n FROM enforcement_actor_links"
        ).fetchone()["n"]
    return {
        "total_cases": total_cases,
        "attempted": attempted,
        "links_created": links,
        "unique_linked_actors": unique_actors,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run_match_pass(), indent=2))
    print(json.dumps(match_summary(), indent=2))
    print()
    print("=== Active defendants (still trading) ===")
    for d in active_defendants(limit=10):
        print(f"  {d['actor_label']} | {d['case_id']} ({d['case_filed_date']}) "
              f"conf={d['match_confidence']}")
