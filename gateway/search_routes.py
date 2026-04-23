"""Unified search across markets, sources, predictions (+ users for admins).

Routes:
  GET  /api/search                 — mixed-type search with FTS5 prefix match
  POST /api/search/click           — log which result was clicked (analytics)
  GET  /admin/search-analytics     — admin dashboard: top queries, zero-result
                                     rate, no-click rate, last 7d

Backed by FTS5 virtual tables (see migrations 115–116) for markets,
sources, and predictions. Users searched via a direct LIKE because the
users table has no rich text worth indexing — email + username and that's
it. Admin-gated behind the is_admin check.

Cache: 30s per (q, types, is_admin, user_id) via the process-local
TTL cache. Keeps repeat keystrokes in the palette off SQLite.
"""

from __future__ import annotations

import html
import logging
import re
import sqlite3
import time
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import db
from cache import ttl_cache
try:
    # Rate limiter lives in the security package; import via try so the
    # module still loads under minimal test fixtures that stub the package.
    from security.rate_limiter import rate_limit, get_client_ip
except Exception:  # pragma: no cover
    def rate_limit(**kwargs):  # type: ignore
        def _wrap(fn):
            return fn
        return _wrap

    def get_client_ip(request):  # type: ignore
        return (request.headers.get("x-forwarded-for") or
                request.client.host if request.client else "unknown")


log = logging.getLogger("gateway.search_routes")


# FTS5 snippet() delimiters — kept in a constant so the CSS selector
# (.narve-mark / <mark>) and the SQL literal agree in one place.
_MARK_OPEN = "<mark>"
_MARK_CLOSE = "</mark>"
# snippet(table, col, start, end, ellipsis, tokens). 15-token window is
# enough for palette subtitles without blowing up the payload.
_SNIPPET_TOKENS = 15


# ── Helpers ─────────────────────────────────────────────────────────────────


_FTS_STRIP_RE = re.compile(r"""['"\-:*()<>^~+!]""")


def _escape_fts(q: str) -> str:
    """Sanitise user input before feeding to FTS5 MATCH.

    FTS5 treats several characters as operators (`-`, `:`, `*`, `"`, `()`
    etc.). If a user types `-rate-hike:yes` we don't want to run that as
    a NOT-AND-column filter — we want to search for those words. Strip
    every operator character, collapse whitespace.
    """
    cleaned = _FTS_STRIP_RE.sub(" ", q or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _fts_prefix_query(q: str) -> str:
    """Build a prefix-matching MATCH string: last term gets `*` so typing
    mid-word still returns results. Earlier terms are AND-joined as
    full terms.

    Examples:
      "fed"        → "fed*"
      "fed rate"   → "fed rate*"
      "rate hike"  → "rate hike*"
    """
    q = _escape_fts(q)
    if not q:
        return ""
    parts = q.split()
    if not parts:
        return ""
    parts[-1] = parts[-1] + "*"
    return " ".join(parts)


def _search_rate_key(request: Request) -> str:
    """Rate-limit bucket: per-user for authed, per-IP for anon.

    Anonymous hits can legitimately reach /api/search (public palette has
    no gate); bucketing on IP keeps a noisy network from drowning the
    authed fleet.
    """
    try:
        import sys
        srv = sys.modules.get("server")
        user = srv.current_user(request) if srv else None
        if user:
            return f"search:user:{user['user_id']}"
    except Exception:
        pass
    return f"search:ip:{get_client_ip(request)}"


def _current_user(request: Request) -> Optional[dict]:
    """Lazy import of server.current_user so this module stays import-safe
    when loaded before server.py finishes initialising."""
    try:
        import sys
        srv = sys.modules.get("server")
        if srv is None:
            return None
        return srv.current_user(request)
    except Exception:
        return None


def _is_admin(user: Optional[dict]) -> bool:
    return bool(user and user.get("is_admin"))


def _log_query(
    user_id: Optional[int],
    query: str,
    result_count: int,
) -> Optional[int]:
    """Write one row to search_queries. Returns rowid for later click-logging.

    Fail-soft: if the table doesn't exist yet (migration not applied) we
    log a warning and return None. Search should never 500 because
    analytics is down.
    """
    try:
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO search_queries (user_id, query, result_count, ts) "
                "VALUES (?, ?, ?, ?)",
                (user_id, query[:500], result_count, int(time.time())),
            )
            return cur.lastrowid
    except sqlite3.Error as exc:
        log.warning("search_queries insert failed: %s", exc)
        return None


# ── Search endpoints ────────────────────────────────────────────────────────


# Per-user / per-IP rate limit. 120/min is generous for palette typing
# (debounced at 150ms ≈ 400/min max from one user) but cuts off scraping
# by a few orders of magnitude.
@rate_limit(limit=120, window_seconds=60, key_func=_search_rate_key)
async def unified_search(request: Request):
    """Main palette endpoint. Returns mixed-type results ranked per FTS5 BM25.

    Query params:
      q       required, ≥2 chars
      types   csv of {markets, sources, predictions, users}; default 'all'
      limit   per-type cap (1–50); default 20
    """
    q_raw = (request.query_params.get("q") or "").strip()
    types = (request.query_params.get("types") or "all").strip().lower()
    try:
        limit = max(1, min(50, int(request.query_params.get("limit") or 20)))
    except ValueError:
        limit = 20

    if len(q_raw) < 2:
        return JSONResponse({"results": [], "query": q_raw, "cached": False})

    user = _current_user(request)
    admin = _is_admin(user)
    uid = (user or {}).get("user_id")
    cache_key = f"search:q_{q_raw[:100]}:t_{types}:adm_{int(admin)}:lim_{limit}"

    def _compute() -> dict:
        fts_q = _fts_prefix_query(q_raw)
        if not fts_q:
            return {"results": [], "query": q_raw, "cached": False}
        results: list[dict] = []
        requested = set(types.split(",")) if types != "all" else {
            "markets", "sources", "predictions"
        }
        if admin and (types == "all" or "users" in (types.split(",") if types != "all" else [])):
            requested.add("users")

        with db.conn() as c:
            if "markets" in requested or types == "all":
                try:
                    # markets_fts column order: 0=market_slug, 1=market_question,
                    # 2=category (see db.init_db). snippet() on col 1 wraps
                    # matched terms in <mark>…</mark> for palette rendering.
                    rows = c.execute(
                        "SELECT ms.market_slug, ms.market_question, ms.category, "
                        f"       snippet(markets_fts, 1, ?, ?, '…', {_SNIPPET_TOKENS}) AS hl "
                        "FROM markets_fts f "
                        "JOIN market_snapshots ms ON ms.rowid = f.rowid "
                        "WHERE markets_fts MATCH ? ORDER BY rank LIMIT ?",
                        (_MARK_OPEN, _MARK_CLOSE, fts_q, limit),
                    ).fetchall()
                    seen_slugs: set[str] = set()
                    for r in rows:
                        slug = r["market_slug"]
                        if slug in seen_slugs:
                            continue  # snapshots table is append-per-crawl
                        seen_slugs.add(slug)
                        results.append({
                            "type": "market",
                            "id": slug,
                            "title": r["market_question"] or slug,
                            "title_html": r["hl"] or r["market_question"] or slug,
                            "subtitle": r["category"] or "",
                            "url": f"/markets/{slug}",
                        })
                except sqlite3.Error as exc:
                    log.warning("markets_fts search failed: %s", exc)

            if "sources" in requested or types == "all":
                # Two FTS sources for sources: handle lookup + rich summary.
                # Dedupe by handle, prefer the summary row (richer subtitle).
                source_by_handle: dict[str, dict] = {}
                try:
                    # source_summaries_fts: col 0=source_handle (UNINDEXED),
                    # col 1=summary. Highlight the summary snippet.
                    rows = c.execute(
                        "SELECT source_handle, summary, "
                        f"       snippet(source_summaries_fts, 1, ?, ?, '…', {_SNIPPET_TOKENS}) AS hl "
                        "FROM source_summaries_fts "
                        "WHERE source_summaries_fts MATCH ? ORDER BY rank LIMIT ?",
                        (_MARK_OPEN, _MARK_CLOSE, fts_q, limit),
                    ).fetchall()
                    for r in rows:
                        h = r["source_handle"]
                        source_by_handle[h] = {
                            "type": "source",
                            "id": h,
                            "title": f"@{h}",
                            "subtitle": (r["summary"] or "")[:140],
                            "subtitle_html": r["hl"] or "",
                            "url": f"/sources/{h}",
                        }
                except sqlite3.Error as exc:
                    log.warning("source_summaries_fts search failed: %s", exc)
                try:
                    # sources_fts: col 0=source_handle (only column). Highlight
                    # the handle itself — useful for "sbf" finding "@sbfwatch".
                    rows = c.execute(
                        "SELECT sc.source_handle, "
                        f"       snippet(sources_fts, 0, ?, ?, '…', {_SNIPPET_TOKENS}) AS hl "
                        "FROM sources_fts f "
                        "JOIN source_credibility sc ON sc.rowid = f.rowid "
                        "WHERE sources_fts MATCH ? ORDER BY rank LIMIT ?",
                        (_MARK_OPEN, _MARK_CLOSE, fts_q, limit),
                    ).fetchall()
                    for r in rows:
                        h = r["source_handle"]
                        if h in source_by_handle:
                            continue  # richer summary hit already present
                        source_by_handle[h] = {
                            "type": "source",
                            "id": h,
                            "title": f"@{h}",
                            "title_html": f"@{r['hl']}" if r["hl"] else f"@{h}",
                            "subtitle": "",
                            "url": f"/sources/{h}",
                        }
                except sqlite3.Error as exc:
                    log.warning("sources_fts search failed: %s", exc)
                results.extend(list(source_by_handle.values())[:limit])

            if "predictions" in requested or types == "all":
                try:
                    # predictions_fts col 0=content. Highlight the matched
                    # span of the prediction text.
                    rows = c.execute(
                        "SELECT p.id, p.content, p.source_handle, p.category, "
                        f"       snippet(predictions_fts, 0, ?, ?, '…', {_SNIPPET_TOKENS}) AS hl "
                        "FROM predictions_fts f "
                        "JOIN predictions p ON p.id = f.rowid "
                        "WHERE predictions_fts MATCH ? ORDER BY rank LIMIT ?",
                        (_MARK_OPEN, _MARK_CLOSE, fts_q, limit),
                    ).fetchall()
                    for r in rows:
                        text = (r["content"] or "")[:200]
                        results.append({
                            "type": "prediction",
                            "id": str(r["id"]),
                            "title": text,
                            "title_html": r["hl"] or text,
                            "subtitle": f"@{r['source_handle']} · {r['category']}",
                            "url": f"/predictions/{r['id']}",
                        })
                except sqlite3.Error as exc:
                    log.warning("predictions_fts search failed: %s", exc)

            if admin and "users" in requested:
                # Users: no rich FTS — direct LIKE on email + username.
                # Bounded by limit so an admin typing a common letter
                # doesn't pull 10k rows.
                like = f"%{q_raw.lower()}%"
                try:
                    rows = c.execute(
                        "SELECT id, email, username FROM users "
                        "WHERE LOWER(email) LIKE ? OR LOWER(username) LIKE ? "
                        "LIMIT ?",
                        (like, like, limit),
                    ).fetchall()
                    for r in rows:
                        results.append({
                            "type": "user",
                            "id": str(r["id"]),
                            "title": r["email"] or r["username"],
                            "subtitle": f"@{r['username']}" if r["username"] else "",
                            "url": f"/admin/users/{r['id']}",
                        })
                except sqlite3.Error as exc:
                    log.warning("users search failed: %s", exc)

        return {"results": results, "query": q_raw}

    # 30s cache of the results payload; analytics run below on every request
    # regardless of cache state so zero-result queries always register.
    payload = ttl_cache.get_or_compute(cache_key, _compute, 30)

    query_id = _log_query(uid, q_raw, len(payload.get("results", [])))
    response_body = dict(payload)
    response_body["query_id"] = query_id
    return JSONResponse(response_body)


# Click logging gets its own bucket so mass-clicks can't burn the search
# quota. 180/min is 3 per second which is absurd for a human but
# plausible if a test harness clicks every row in a batch result.
@rate_limit(limit=180, window_seconds=60, key_func=_search_rate_key)
async def log_click(request: Request):
    """Log which result got clicked. POST body:
      {"query_id": int, "result_type": str, "result_id": str}

    query_id comes from the /api/search response; front-end keeps it in
    memory between search and click. Fail-soft on bad input — we never
    want click logging to break the navigation flow.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        query_id = int(body.get("query_id") or 0)
    except (TypeError, ValueError):
        query_id = 0
    rtype = (body.get("result_type") or "")[:32]
    rid = (body.get("result_id") or "")[:200]
    if not query_id or not rtype:
        # Return 200 so the client doesn't see failed click-logging as an
        # error; analytics is best-effort.
        return JSONResponse({"logged": False})
    try:
        with db.conn() as c:
            c.execute(
                "UPDATE search_queries "
                "SET clicked_result_type = ?, clicked_result_id = ?, clicked_at = ? "
                "WHERE id = ?",
                (rtype, rid, int(time.time()), query_id),
            )
    except sqlite3.Error as exc:
        log.warning("search_queries click update failed: %s", exc)
        return JSONResponse({"logged": False})
    return JSONResponse({"logged": True})


# ── Popular queries (public, aggregated) ───────────────────────────────────


# Longer TTL than results — popular set shifts slowly, and every palette
# open fires this endpoint. 5 min is plenty.
_POPULAR_TTL_SECONDS = 300
_POPULAR_MIN_COUNT = 3  # must have been searched ≥3× to qualify


@rate_limit(limit=120, window_seconds=60, key_func=_search_rate_key)
async def popular_queries(request: Request):
    """Top non-zero-result queries from the last 7 days, aggregated.

    Rendered in the palette's empty-state so first-time visitors have a
    starting point. Filters:
      * query length ≥ 3 (nothing like single letters)
      * count ≥ _POPULAR_MIN_COUNT (can't leak a single admin's typo)
      * excludes queries containing '@' (those are user-lookup attempts
        from admins and may echo private handles)

    No PII, no admin gating — the set is naturally k-anonymous via the
    min-count floor. Aggregated through the TTL cache to keep SQLite
    off the per-open hot path.
    """
    def _compute() -> dict:
        since = int(time.time()) - 7 * 86400
        try:
            with db.conn() as c:
                rows = c.execute(
                    "SELECT query, COUNT(*) AS n "
                    "FROM search_queries "
                    "WHERE ts >= ? AND result_count > 0 "
                    "  AND LENGTH(query) >= 3 AND query NOT LIKE '%@%' "
                    "GROUP BY query "
                    "HAVING n >= ? "
                    "ORDER BY n DESC, query ASC "
                    "LIMIT 6",
                    (since, _POPULAR_MIN_COUNT),
                ).fetchall()
                return {"queries": [r["query"] for r in rows]}
        except sqlite3.Error as exc:
            log.warning("popular_queries failed: %s", exc)
            return {"queries": []}

    payload = ttl_cache.get_or_compute(
        "search:popular:7d", _compute, _POPULAR_TTL_SECONDS,
    )
    return JSONResponse(payload)


# ── Admin analytics ─────────────────────────────────────────────────────────


def _require_admin(request: Request) -> dict:
    user = _current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def admin_search_analytics(request: Request):
    """Top queries last 7d, zero-result rate, no-click rate."""
    _require_admin(request)

    since = int(time.time()) - 7 * 86400
    top_queries: list[dict] = []
    zero_result: list[dict] = []
    summary: dict[str, Any] = {"total": 0, "zero_count": 0, "no_click_count": 0}
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT query, COUNT(*) AS n, "
                "       AVG(result_count) AS avg_results, "
                "       SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicks "
                "FROM search_queries WHERE ts >= ? "
                "GROUP BY query ORDER BY n DESC LIMIT 20",
                (since,),
            ).fetchall()
            top_queries = [dict(r) for r in rows]

            rows = c.execute(
                "SELECT query, COUNT(*) AS n FROM search_queries "
                "WHERE ts >= ? AND result_count = 0 "
                "GROUP BY query ORDER BY n DESC LIMIT 20",
                (since,),
            ).fetchall()
            zero_result = [dict(r) for r in rows]

            row = c.execute(
                "SELECT COUNT(*) AS total, "
                "       SUM(CASE WHEN result_count = 0 THEN 1 ELSE 0 END) AS zero_count, "
                "       SUM(CASE WHEN clicked_at IS NULL AND result_count > 0 THEN 1 ELSE 0 END) AS no_click_count "
                "FROM search_queries WHERE ts >= ?",
                (since,),
            ).fetchone()
            if row:
                summary = dict(row)
                for k in ("total", "zero_count", "no_click_count"):
                    summary[k] = summary.get(k) or 0
    except sqlite3.Error as exc:
        log.warning("admin_search_analytics query failed: %s", exc)

    zero_rate = (summary["zero_count"] / summary["total"]) if summary["total"] else 0.0
    no_click_rate = (
        summary["no_click_count"] / max(summary["total"] - summary["zero_count"], 1)
    )

    def _row(r: dict, kind: str) -> str:
        q = html.escape(r.get("query") or "")
        if kind == "top":
            avg = float(r.get("avg_results") or 0)
            clicks = int(r.get("clicks") or 0)
            n = int(r.get("n") or 0)
            return (
                f"<tr><td><code>{q}</code></td>"
                f"<td class='num'>{n}</td>"
                f"<td class='num'>{avg:.1f}</td>"
                f"<td class='num'>{clicks}</td></tr>"
            )
        return (
            f"<tr><td><code>{q}</code></td>"
            f"<td class='num'>{int(r.get('n') or 0)}</td></tr>"
        )

    top_html = "".join(_row(r, "top") for r in top_queries) or (
        "<tr><td colspan='4' class='muted'>No searches in last 7 days.</td></tr>"
    )
    zero_html = "".join(_row(r, "zero") for r in zero_result) or (
        "<tr><td colspan='2' class='muted'>No zero-result searches — nice.</td></tr>"
    )

    body = f"""<!DOCTYPE html><html lang='en'><head>
<meta charset='utf-8'><title>Search analytics — narve admin</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>
body{{background:var(--bg-base);color:var(--text-primary);
font-family:var(--font-ui);padding:40px;max-width:1000px;margin:0 auto}}
h1{{font-family:var(--font-display);font-style:italic;font-size:40px;
margin:0 0 8px;letter-spacing:-0.02em}}
.meta{{color:var(--text-tertiary);font-size:12px;font-family:var(--font-mono);
text-transform:uppercase;letter-spacing:0.1em;margin-bottom:32px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:32px}}
.card{{background:var(--bg-raised);border:1px solid var(--border-default);
border-radius:12px;padding:16px}}
.card-label{{font-size:11px;color:var(--text-tertiary);text-transform:uppercase;
letter-spacing:0.08em;margin:0 0 8px;font-family:var(--font-mono)}}
.card-value{{font-size:28px;font-weight:500;margin:0;font-variant-numeric:tabular-nums}}
h2{{font-family:var(--font-display);font-style:italic;font-size:24px;margin:32px 0 8px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border-subtle)}}
th{{color:var(--text-tertiary);font-size:11px;text-transform:uppercase;letter-spacing:0.08em;
font-family:var(--font-mono);font-weight:500}}
.num{{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--font-mono)}}
code{{font-family:var(--font-mono);font-size:12px;color:var(--text-primary)}}
.muted{{color:var(--text-tertiary);text-align:center;padding:24px;font-style:italic}}
</style></head><body>
<h1>Search analytics</h1>
<p class='meta'>Unified ⌘K search · last 7 days</p>

<div class='grid'>
  <div class='card'><p class='card-label'>Total queries</p>
    <p class='card-value'>{summary['total']:,}</p></div>
  <div class='card'><p class='card-label'>Zero-result rate</p>
    <p class='card-value'>{zero_rate * 100:.1f}%</p></div>
  <div class='card'><p class='card-label'>No-click rate (non-zero)</p>
    <p class='card-value'>{no_click_rate * 100:.1f}%</p></div>
</div>

<h2>Top queries</h2>
<table>
  <thead><tr><th>Query</th><th class='num'>Searches</th>
    <th class='num'>Avg results</th><th class='num'>Clicks</th></tr></thead>
  <tbody>{top_html}</tbody>
</table>

<h2>Zero-result queries</h2>
<p style='color:var(--text-secondary);font-size:13px;margin:0 0 8px'>
  Content users look for that we don't have. Candidates for new
  categories, alias entries, or product gaps.</p>
<table>
  <thead><tr><th>Query</th><th class='num'>Searches</th></tr></thead>
  <tbody>{zero_html}</tbody>
</table>
</body></html>"""
    return HTMLResponse(body)


# ── Registration ────────────────────────────────────────────────────────────


def register(app) -> None:
    """Wire unified-search routes into the given FastAPI app."""
    app.add_api_route("/api/search", unified_search, methods=["GET"],
                      include_in_schema=False)
    app.add_api_route("/api/search/popular", popular_queries, methods=["GET"],
                      include_in_schema=False)
    app.add_api_route("/api/search/click", log_click, methods=["POST"],
                      include_in_schema=False)
    app.add_api_route("/admin/search-analytics", admin_search_analytics,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
