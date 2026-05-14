"""Changelog parsing + per-user "seen" state + JSON API + RSS feed.

Endpoints:
  GET  /api/changelog          — recent entries (public, cached 5 min)
  POST /api/changelog/seen     — mark entry_keys as seen for the current user
  GET  /changelog.rss          — RSS 2.0 feed of every changelog entry

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

import datetime as _dt
import hashlib
import html as _html
import logging
import re
import time
from email import utils as _email_utils
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

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
# Friendlier "## Week of YYYY-MM-DD" form (now canonical in CHANGELOG.md).
# Matched in addition to the bracketed [version] form so legacy keepachangelog
# entries still parse without rewriting them. Version is set to "Week of <date>"
# so downstream consumers (entry_key, summary, etc.) still see a unique label.
_WEEK_HEADER_RE = re.compile(
    r"^##\s+Week\s+of\s+(\d{4}-\d{2}-\d{2})\s*$",
    re.IGNORECASE,
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
          "raw_sections": {"Added": ["**Foo** bar", ...], ...},
        }

    ``sections`` carries plain-text bullets (markdown stripped) for the
    legacy widget contract; ``raw_sections`` carries the original
    markdown so the HTML/RSS renderer can show **bold**, `code`, and
    [link](url) inline.

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
        wm = None if m else _WEEK_HEADER_RE.match(line)
        if m or wm:
            if current is not None:
                entries.append(_finalise(current))
            if m:
                version = m.group(1).strip()
                date = (m.group(2) or "").strip() or None
            else:
                # "## Week of YYYY-MM-DD" — surface the date in both fields so
                # downstream consumers (widget, page, RSS) get a date AND a
                # human-meaningful version string at once.
                date = wm.group(1).strip()
                version = f"Week of {date}"
            current = {
                "key": _entry_key(version, date),
                "version": version,
                "date": date,
                # ``sections`` is the historical contract (plain-text used by
                # the JSON API + widget); ``raw_sections`` preserves the
                # original markdown so the HTML/RSS renderer can show
                # **bold** / `code` / links.
                "sections": {},
                "raw_sections": {},
            }
            current_section = None
            continue

        if current is None:
            continue  # preamble before first ## header

        m = _SECTION_HEADER_RE.match(line)
        if m:
            current_section = m.group(1).strip()
            current["sections"].setdefault(current_section, [])
            current["raw_sections"].setdefault(current_section, [])
            continue

        if current_section is None:
            continue

        m = _BULLET_RE.match(line)
        if m:
            raw = m.group(1).strip()
            current["sections"][current_section].append(_strip_md(raw))
            current["raw_sections"][current_section].append(raw)
        elif line.strip() and current["sections"].get(current_section):
            # Continuation of the previous bullet — append a space + line so
            # paragraph-style bullets stay readable.
            current["sections"][current_section][-1] += " " + _strip_md(line)
            current["raw_sections"][current_section][-1] += (
                " " + line.strip()
            )

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


# ── HTML + RSS rendering ────────────────────────────────────────────────────
#
# Lightweight markdown→HTML for bullet text: handles **bold**, `code`, and
# [link](url) — the three formats that actually appear in CHANGELOG.md. A
# full markdown library is overkill (and would re-parse the file) when the
# bullet vocabulary is this small. Output is HTML-escaped first, then the
# inline tokens get their tags added back, so user-supplied changelog text
# can never escape the rendering context.


_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^\*]+)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


# Section labels we render as their own card sub-block, in canonical order.
# Anything unknown still renders (we don't drop it), but appears at the end
# under its raw label so a misspelled heading is visible to the operator.
_SECTION_ORDER = ("Added", "Changed", "Fixed", "Removed", "Security",
                  "Deprecated")
_SECTION_KIND = {
    "Added":      "added",
    "Changed":    "changed",
    "Fixed":      "fixed",
    "Removed":    "removed",
    "Security":   "security",
    "Deprecated": "deprecated",
}


def _safe_url(url: str) -> str:
    """Allow http(s), mailto, and relative links; reject anything else so
    a malicious ``[text](javascript:...)`` bullet can't ship XSS."""
    u = (url or "").strip()
    if u.startswith(("http://", "https://", "mailto:", "/", "#")):
        return u
    return ""


def _render_bullet_html(text: str) -> str:
    """Render one bullet's inline markdown to safe HTML."""
    s = _html.escape(text, quote=False)
    # Code spans first so their content isn't re-processed for bold/links.
    s = _INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", s)
    s = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", s)

    def _link_sub(m: "re.Match") -> str:
        label = m.group(1)
        href = _safe_url(m.group(2))
        if not href:
            return label
        return f'<a href="{href}" rel="noopener">{label}</a>'

    s = _LINK_RE.sub(_link_sub, s)
    return s


def _render_section_bullets(bullets: list[str]) -> str:
    """Render a section's bullets as an ``<ul>``."""
    if not bullets:
        return ""
    items = "".join(f"<li>{_render_bullet_html(b)}</li>" for b in bullets)
    return f'<ul class="cl-bullets">{items}</ul>'


def _relative_time(date_str: Optional[str],
                   now: Optional[_dt.date] = None) -> str:
    """Human-readable distance from ``date_str`` (YYYY-MM-DD) to ``now``.

    Returns short forms like "today", "3 days ago", "last week", "2 weeks
    ago", "3 months ago". Falls back to "" if the date can't be parsed —
    callers should not render the chip in that case.
    """
    if not date_str:
        return ""
    try:
        d = _dt.date.fromisoformat(date_str.strip())
    except ValueError:
        return ""
    today = now or _dt.date.today()
    delta = (today - d).days
    if delta < 0:
        return "scheduled"
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta < 7:
        return f"{delta} days ago"
    if delta < 14:
        return "last week"
    if delta < 30:
        return f"{delta // 7} weeks ago"
    if delta < 60:
        return "last month"
    if delta < 365:
        return f"{delta // 30} months ago"
    if delta < 730:
        return "last year"
    return f"{delta // 365} years ago"


def _anchor_id(entry: dict[str, Any]) -> str:
    """Anchor id for an entry: ``week-YYYY-MM-DD`` when dated, else
    ``entry-<key>`` so Unreleased / non-dated blocks are still
    deep-linkable."""
    date = entry.get("date")
    if date:
        return f"week-{date}"
    return f"entry-{entry.get('key', '')}"


def render_entry_html(entry: dict[str, Any]) -> str:
    """Render a single parsed entry as the polished card HTML."""
    # Prefer raw_sections (preserves **bold** / `code` / [link](url) so the
    # markdown renderer has something to render); fall back to the stripped
    # `sections` for old fixture payloads that don't carry the raw form.
    sections = entry.get("raw_sections") or entry.get("sections") or {}
    version = entry.get("version") or "Unreleased"
    date = entry.get("date")
    anchor = _anchor_id(entry)
    chip_label = _relative_time(date) if date else "unreleased"
    chip_html = (
        f'<span class="cl-chip cl-chip--time">{_html.escape(chip_label)}</span>'
        if chip_label else ""
    )
    date_html = (
        f'<time class="cl-card__date" datetime="{_html.escape(date)}">'
        f"{_html.escape(date)}</time>" if date else ""
    )

    # Ordered sections first, then anything unrecognised so missing labels
    # don't silently vanish — keeps the operator honest about what shipped.
    rendered_labels: set = set()
    section_blocks: list[str] = []
    for label in _SECTION_ORDER:
        bullets = sections.get(label) or []
        if not bullets:
            continue
        rendered_labels.add(label)
        kind = _SECTION_KIND.get(label, "other")
        section_blocks.append(
            f'<section class="cl-section cl-section--{kind}">'
            f'<h3 class="cl-section__heading">'
            f'<span class="cl-chip cl-chip--label cl-chip--{kind}">'
            f"{_html.escape(label)}</span></h3>"
            f"{_render_section_bullets(bullets)}"
            f"</section>"
        )
    for label, bullets in sections.items():
        if label in rendered_labels or not bullets:
            continue
        section_blocks.append(
            '<section class="cl-section cl-section--other">'
            f'<h3 class="cl-section__heading">'
            f'<span class="cl-chip cl-chip--label cl-chip--other">'
            f"{_html.escape(label)}</span></h3>"
            f"{_render_section_bullets(bullets)}"
            "</section>"
        )

    return (
        f'<article class="cl-card" id="{_html.escape(anchor)}">'
        f'<header class="cl-card__head">'
        f'<h2 class="cl-card__title">{_html.escape(version)}</h2>'
        f"{date_html}{chip_html}"
        "</header>"
        f"{''.join(section_blocks)}"
        "</article>"
    )


def render_changelog_html(
    entries: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Render every parsed entry as a stack of cards (newest first)."""
    if entries is None:
        entries = _parsed_entries()
    if not entries:
        return (
            '<p class="cl-empty">No changelog entries yet — '
            "check back after the next deploy.</p>"
        )
    return "\n".join(render_entry_html(e) for e in entries)


def _rfc822(date_str: Optional[str]) -> str:
    """RFC822 timestamp for an RSS ``<pubDate>``.

    The CHANGELOG entries are "Week of YYYY-MM-DD"; we anchor each item at
    00:00 UTC of that date. RFC822 strictly requires day-of-week and a
    timezone offset, both of which ``email.utils.format_datetime`` emits.
    Falls back to "now" if parsing fails so the feed still validates.
    """
    try:
        d = _dt.date.fromisoformat((date_str or "").strip())
        ts = _dt.datetime(d.year, d.month, d.day, 0, 0, 0,
                          tzinfo=_dt.timezone.utc)
    except Exception:
        ts = _dt.datetime.now(tz=_dt.timezone.utc)
    return _email_utils.format_datetime(ts)


def render_rss(entries: Optional[list[dict[str, Any]]] = None,
               *, base_url: str = "https://narve.ai") -> str:
    """Render the parsed entries as an RSS 2.0 feed.

    One ``<item>`` per entry; description is CDATA-wrapped HTML so feed
    readers can render bold/code/links. Channel ``<lastBuildDate>`` is
    set to the most recent entry's pubDate so caching proxies behave.
    """
    if entries is None:
        entries = _parsed_entries()

    items: list[str] = []
    most_recent: Optional[str] = None
    for e in entries:
        date = e.get("date")
        version = e.get("version") or "Unreleased"
        title = (
            f"narve.ai update — Week of {date}" if date
            else f"narve.ai update — {version}"
        )
        anchor = _anchor_id(e)
        link = f"{base_url}/changelog#{anchor}"
        # GUID: stable per-entry, non-permalink (since the link is an
        # anchor, not a unique URL). Falls back to the parser's hash key
        # for entries without a date so each Unreleased block still has a
        # distinct guid across deploys.
        if date:
            guid = f"narve-changelog-{date}"
        else:
            guid = f"narve-changelog-{e.get('key', 'unknown')}"
        pub_date = _rfc822(date)
        if most_recent is None:
            most_recent = pub_date
        # Description: render each section into HTML, wrapped in CDATA so
        # the XML parser doesn't need to escape the markup. Split any
        # accidental ``]]>`` so it can't terminate the CDATA early.
        sections = e.get("raw_sections") or e.get("sections") or {}
        section_blocks: list[str] = []
        for label in _SECTION_ORDER:
            bullets = sections.get(label) or []
            if not bullets:
                continue
            section_blocks.append(
                f"<h3>{_html.escape(label)}</h3>"
                f"{_render_section_bullets(bullets)}"
            )
        for label, bullets in sections.items():
            if label in _SECTION_ORDER or not bullets:
                continue
            section_blocks.append(
                f"<h3>{_html.escape(label)}</h3>"
                f"{_render_section_bullets(bullets)}"
            )
        description_html = "".join(section_blocks) or "<p>(no notes)</p>"
        description_html = description_html.replace("]]>", "]]]]><![CDATA[>")

        items.append(
            "<item>"
            f"<title>{_html.escape(title)}</title>"
            f"<link>{_html.escape(link)}</link>"
            f'<guid isPermaLink="false">{_html.escape(guid)}</guid>'
            f"<pubDate>{pub_date}</pubDate>"
            f"<description><![CDATA[{description_html}]]></description>"
            "</item>"
        )

    last_build = most_recent or _email_utils.format_datetime(
        _dt.datetime.now(tz=_dt.timezone.utc)
    )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel>"
        "<title>narve.ai changelog</title>"
        f"<link>{base_url}/changelog</link>"
        "<description>Product updates and release notes for narve.ai."
        "</description>"
        "<language>en-us</language>"
        f'<atom:link href="{base_url}/changelog.rss" rel="self" '
        'type="application/rss+xml" />'
        f"<lastBuildDate>{last_build}</lastBuildDate>"
        f"{''.join(items)}"
        "</channel></rss>"
    )


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


async def changelog_rss(request: Request) -> Response:
    """GET /changelog.rss — RSS 2.0 feed of every parsed changelog entry.

    Cached for 1 hour (entries only change on deploy). Feed readers fetch
    anonymously, so the path is added to ``_PUBLIC_PATHS`` in server.py
    alongside the other SEO routes so the gate doesn't 401 them.
    """
    # Use the request's host so subdomain crawls return the right URLs in
    # ``<link>``/``<guid>``. Fall back to the apex if the header is missing.
    host = request.headers.get("host") or "narve.ai"
    scheme = (
        "http" if host.startswith("localhost") or host.startswith("127.")
        else "https"
    )
    base_url = f"{scheme}://{host}"
    body = render_rss(base_url=base_url)
    headers = {
        "Cache-Control": "public, max-age=3600",
        "Content-Type": "application/rss+xml; charset=utf-8",
    }
    return Response(content=body, headers=headers,
                    media_type="application/rss+xml")


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
    app.add_api_route(
        "/changelog.rss", changelog_rss,
        methods=["GET"], include_in_schema=False,
    )
