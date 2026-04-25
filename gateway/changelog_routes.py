"""Changelog parsing + per-user "seen" state + JSON API.

Two endpoints:
  GET  /api/changelog          — recent entries (public, cached 5 min)
  POST /api/changelog/seen     — mark entry_keys as seen for the current user

The changelog source is `CHANGELOG.md` at the repo root, in
keepachangelog format. We parse it once per process and cache the
parsed entries in-process — they only change on deploy, and we don't
need an admin button to invalidate (a process restart suffices).

`entry_key` is a stable id derived from version + date so the same
entry returns the same key across parses; that's what
`changelog_seen` (migration 170) keys off.

Registered into the FastAPI app via `register(app)` from server.py at
import time, mirroring `admin_routes.py`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import db


log = logging.getLogger("gateway.changelog")


# Repo root → /CHANGELOG.md. gateway/ lives one level below.
_REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG_PATH = _REPO_ROOT / "CHANGELOG.md"


# In-process parse cache — invalidated on file mtime change so the
# admin can ship a hot-reload of the changelog without a full restart
# (rsync the file, parse cache notices the new mtime, refreshes).
_parse_cache: dict[str, Any] = {"mtime": 0.0, "entries": []}


# ── Parser ──────────────────────────────────────────────────────────────────


_VERSION_HEADER_RE = re.compile(
    r"^##\s*\[([^\]]+)\]\s*(?:-\s*(\d{4}-\d{2}-\d{2})\s*)?\s*$",
)
_SECTION_HEADER_RE = re.compile(r"^###\s*(.+?)\s*$")
_BULLET_RE = re.compile(r"^[\-\*]\s+(.+?)\s*$")


def _entry_key(version: str, date: Optional[str]) -> str:
    """Stable id: sha1(version + '|' + date) → 12 hex. Hashing instead
    of using the version verbatim keeps the key URL-safe and prevents
    weird strings like ``[Unreleased]`` from becoming opaque keys."""
    base = f"{version.strip()}|{(date or '').strip()}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def _strip_md(line: str) -> str:
    """Cheap markdown→plain — drop ** / __ / `code` markers and
    collapse adjacent whitespace. Keeps the body summary readable in
    the widget without rendering full markdown there."""
    s = line
    s = re.sub(r"\*\*([^\*]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", s)  # [text](url) → text
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def parse_changelog(text: Optional[str] = None) -> list[dict[str, Any]]:
    """Parse keepachangelog markdown into a list of entries.

    Each entry::

        {
          "key": "9f3c4b2a1d77",
          "version": "Unreleased",
          "date": "2026-04-25",          # may be None
          "title": "Most-recent change title",
          "summary": "First sentence of body, plain text",
          "sections": {"Added": [...], "Changed": [...], "Fixed": [...]},
        }

    The newest entry is first. Entries with no bullet items (rare —
    only in newly stubbed releases) get an empty sections dict and
    "(no notes)" as summary so the widget never renders blank rows.
    """
    if text is None:
        try:
            text = CHANGELOG_PATH.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("changelog: cannot read %s: %s", CHANGELOG_PATH, e)
            return []

    entries: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    current_section: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = _VERSION_HEADER_RE.match(line)
        if m:
            if current is not None:
                entries.append(_finalise(current))
            version = m.group(1).strip()
            date = (m.group(2) or "").strip() or None
            current = {
                "key": _entry_key(version, date),
                "version": version,
                "date": date,
                "sections": {},
            }
            current_section = None
            continue

        if current is None:
            continue  # preamble before first ## [version] header

        m = _SECTION_HEADER_RE.match(line)
        if m:
            current_section = m.group(1).strip()
            current["sections"].setdefault(current_section, [])
            continue

        if current_section is None:
            continue

        m = _BULLET_RE.match(line)
        if m:
            current["sections"][current_section].append(_strip_md(m.group(1)))
        elif line.strip() and current["sections"].get(current_section):
            # Continuation of the previous bullet — append a space + line so
            # paragraph-style bullets stay readable.
            current["sections"][current_section][-1] += " " + _strip_md(line)

    if current is not None:
        entries.append(_finalise(current))

    return entries


def _finalise(entry: dict[str, Any]) -> dict[str, Any]:
    """Compute title + summary from sections so the widget has a
    headline shape without each call site having to re-derive it."""
    sections = entry.get("sections") or {}
    # Headline preference: first Added bullet → first Changed → first
    # Fixed → "(no notes)". Matches what users care about most.
    for label in ("Added", "Changed", "Fixed", "Removed", "Security",
                  "Deprecated"):
        bullets = sections.get(label) or []
        if bullets:
            first = bullets[0]
            entry["title"] = first if len(first) <= 100 else first[:97] + "…"
            tail = " · ".join(bullets[1:3])
            entry["summary"] = (
                tail if tail else first if len(first) > 100 else ""
            )
            return entry

    entry["title"] = entry["version"]
    entry["summary"] = "(no notes)"
    return entry


def _parsed_entries() -> list[dict[str, Any]]:
    """Read parse cache, refreshing if CHANGELOG.md mtime changed."""
    try:
        mtime = CHANGELOG_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    if mtime != _parse_cache["mtime"]:
        _parse_cache["entries"] = parse_changelog()
        _parse_cache["mtime"] = mtime
    return _parse_cache["entries"]


# ── DB helpers ──────────────────────────────────────────────────────────────


def get_seen_keys(user_id: int) -> set[str]:
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT entry_key FROM changelog_seen WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return {r["entry_key"] for r in rows}
    except Exception as e:
        log.warning("changelog: get_seen_keys failed: %s", e)
        return set()


def mark_seen(user_id: int, entry_keys: list[str]) -> int:
    """Insert (user_id, entry_key) for every key. Existing rows are
    silently kept — first-seen-at is preserved on replay so you can
    audit the very first impression. Returns the count actually inserted."""
    if not entry_keys:
        return 0
    inserted = 0
    now = int(time.time())
    with db.conn() as c:
        for key in entry_keys:
            key = (key or "").strip()
            if not key:
                continue
            try:
                cur = c.execute(
                    "INSERT OR IGNORE INTO changelog_seen "
                    "(user_id, entry_key, seen_at) VALUES (?, ?, ?)",
                    (user_id, key, now),
                )
                inserted += cur.rowcount or 0
            except Exception as e:
                log.warning("changelog: mark_seen %s/%s failed: %s",
                            user_id, key, e)
    return inserted


# ── Routes ──────────────────────────────────────────────────────────────────


def _current_user(request) -> Optional[dict]:
    """Defer back to server.py's auth helper — same pattern as
    admin_routes._current_user."""
    import sys
    srv = sys.modules.get("server") or sys.modules.get("__main__")
    if not srv:
        return None
    fn = getattr(srv, "current_user", None)
    return fn(request) if fn else None


async def changelog_entries(request: Request, limit: int = 3) -> JSONResponse:
    """Public endpoint — returns the N most recent entries.

    `limit` clamped to [1, 20]. When the request is authenticated, each
    entry's ``seen`` field reflects the user's `changelog_seen` row;
    otherwise ``seen`` is False everywhere.
    """
    limit = max(1, min(int(limit or 3), 20))
    entries = _parsed_entries()[:limit]

    user = _current_user(request)
    seen = get_seen_keys(user["user_id"]) if user else set()

    payload = []
    for e in entries:
        payload.append({
            "key": e["key"],
            "version": e["version"],
            "date": e.get("date"),
            "title": e.get("title", ""),
            "summary": e.get("summary", ""),
            "seen": e["key"] in seen,
        })
    resp = JSONResponse({
        "entries": payload,
        "unseen_count": sum(1 for e in payload if not e["seen"]),
    })
    # 5-minute browser cache — entries change only on deploy.
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


async def changelog_seen_post(request: Request) -> JSONResponse:
    """POST /api/changelog/seen — mark a list of entry_keys as seen.

    Body: ``{"keys": ["abc123", "def456", ...]}`` (also accepts
    ``"entry_keys"`` for symmetry with internal naming). Anonymous
    requests are accepted with a 200 + ``persisted: false`` so the
    widget JS can fire-and-forget without checking auth state first.
    """
    user = _current_user(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_keys = body.get("keys") or body.get("entry_keys") or []
    if not isinstance(raw_keys, list):
        raise HTTPException(status_code=400, detail="keys must be an array")
    keys = [str(k).strip() for k in raw_keys if str(k).strip()]
    keys = keys[:20]  # cheap upper bound — defends against an oversized POST

    if not user:
        return JSONResponse({"persisted": False, "marked": 0})

    inserted = mark_seen(user["user_id"], keys)
    return JSONResponse({"persisted": True, "marked": inserted})


# ── Registration ────────────────────────────────────────────────────────────


def register(app) -> None:
    """Wire the changelog routes into the FastAPI app. Idempotent."""
    app.add_api_route(
        "/api/changelog", changelog_entries,
        methods=["GET"], include_in_schema=False,
    )
    app.add_api_route(
        "/api/changelog/seen", changelog_seen_post,
        methods=["POST"], include_in_schema=False,
    )
