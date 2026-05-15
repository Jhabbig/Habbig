"""Admin /admin/jobs — background-job queue dashboard.

Registered by being imported at the bottom of ``server.py`` (same pattern
as ``admin_health_monitor_routes``, ``status_routes``, etc.).

Routes exposed:
    GET  /admin/jobs                       HTML page (admin shell)
    GET  /admin/api/jobs/refresh           JSON snapshot (polled every 5s)
    GET  /admin/api/jobs                   JSON list (one row per registered job)
    GET  /admin/api/jobs/{name}/history    last 50 runs for a job
    POST /admin/api/jobs/{name}/pause      pause schedule
    POST /admin/api/jobs/{name}/resume     resume schedule
    POST /admin/api/jobs/{name}/trigger    fire now (records triggered_by=admin)

Every route goes through ``server._require_admin_user``. POSTs enforce
the global CSRF middleware — no exemption.
"""

from __future__ import annotations

import datetime as _dt
import html
import logging
import time
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import db
import server
from admin_shell import render_admin_page
from queries import jobs as job_queries
from security.rate_limiter import rate_limit, get_client_ip

log = logging.getLogger("admin_jobs")

# Whitelist of allowed status filter values. ``scheduled`` is a synthetic
# label (no row in ``job_runs`` ever has that status — it's a property of
# the cron registry), so filtering recent runs by ``scheduled`` produces
# an empty set by design. The four labels match what the user-facing UI
# advertises in the filter dropdown.
_JOB_STATUS_LABELS = ("running", "success", "failed", "scheduled")


def _admin_key(request: Request) -> str:
    user = server.current_user(request)
    if user and user.get("is_admin"):
        return f"admin_jobs:{user['user_id']}"
    return f"admin_jobs:anon:{get_client_ip(request)}"


# ── Legacy aggregated per-job stats (kept for backwards compat) ─────────

def _job_stats() -> dict[str, dict]:
    """Aggregate per-job stats. Used by the legacy /admin/api/jobs route.

    The new dashboard reads :mod:`queries.jobs` directly; this is kept
    so any external integration polling /admin/api/jobs keeps working.
    """
    import time as _time
    now = int(_time.time())
    cutoff_24h = now - 86400
    try:
        with db.conn() as c:
            rows = c.execute(
                """
                SELECT
                  job_name,
                  MAX(started_at) AS last_run,
                  (SELECT ok FROM job_runs r2
                     WHERE r2.job_name = r.job_name
                     ORDER BY started_at DESC LIMIT 1) AS last_ok,
                  (SELECT duration_ms FROM job_runs r3
                     WHERE r3.job_name = r.job_name AND duration_ms IS NOT NULL
                     ORDER BY started_at DESC LIMIT 1) AS last_duration_ms,
                  ROUND(AVG(duration_ms)) AS avg_ms,
                  SUM(CASE WHEN ok = 0 AND started_at >= ? THEN 1 ELSE 0 END) AS fail_count_24h,
                  COUNT(*) AS total_runs
                FROM job_runs r
                GROUP BY job_name
                """,
                (cutoff_24h,),
            ).fetchall()
    except Exception:
        log.exception("admin_jobs: stats query failed")
        return {}
    return {row["job_name"]: dict(row) for row in rows}


# ── Rendering helpers (server-side snapshot) ─────────────────────────────

def _esc(s) -> str:
    return html.escape("" if s is None else str(s))


def _fmt_ts(t: Optional[int]) -> str:
    if not t:
        return "—"
    try:
        delta = int(time.time()) - int(t)
    except Exception:
        return "—"
    if delta < 0:
        fwd = -delta
        if fwd < 60: return f"in {fwd}s"
        if fwd < 3600: return f"in {fwd // 60}m"
        if fwd < 86400: return f"in {fwd // 3600}h"
        return f"in {fwd // 86400}d"
    if delta < 60: return f"{delta}s ago"
    if delta < 3600: return f"{delta // 60}m ago"
    if delta < 86400: return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _fmt_duration(ms: Optional[int]) -> str:
    if ms is None:
        return "—"
    try:
        ms = int(ms)
    except Exception:
        return "—"
    if ms < 1000: return f"{ms}ms"
    if ms < 60_000: return f"{ms / 1000:.1f}s"
    return f"{ms // 1000}s"


def _fmt_pct(p: Optional[float]) -> str:
    if p is None: return "—"
    return f"{p:.1f}%"


def _render_running_rows(runs: list[dict]) -> str:
    if not runs:
        return '<tr><td colspan="4" class="jobs-empty">Nothing running right now.</td></tr>'
    parts: list[str] = []
    now = int(time.time())
    for r in runs:
        started = r.get("started_at") or 0
        age_ms = max(0, (now - int(started)) * 1000) if started else None
        parts.append(
            "<tr>"
            f'<td><span class="jobs-cell-name">{_esc(r.get("job_name"))}</span></td>'
            f'<td class="jobs-cell-mono">{_esc(_fmt_ts(started))}</td>'
            f'<td class="jobs-cell-mono">{_esc(_fmt_duration(age_ms))}</td>'
            f'<td class="jobs-cell-mono">{_esc(r.get("triggered_by") or "schedule")}</td>'
            "</tr>"
        )
    return "".join(parts)


def _render_cron_rows(cron: list[dict]) -> str:
    if not cron:
        return '<tr><td colspan="6" class="jobs-empty">No jobs registered. Scheduler may be disabled.</td></tr>'
    parts: list[str] = []
    for c in cron:
        paused = '<span class="jobs-paused-tag">paused</span>' if c.get("paused") else ""
        rate = _fmt_pct(c.get("success_rate_24h"))
        parts.append(
            "<tr>"
            f'<td><span class="jobs-cell-name">{_esc(c.get("name"))}</span>{paused}</td>'
            f'<td><span class="jobs-cell-schedule">{_esc(c.get("schedule"))}</span></td>'
            f'<td class="jobs-cell-mono">{_esc(_fmt_ts(c.get("next_run")))}</td>'
            f'<td class="jobs-cell-mono">{_esc(_fmt_ts(c.get("last_run")))}</td>'
            f'<td class="num">{_esc(rate)}</td>'
            f'<td class="num">{int(c.get("runs_24h") or 0)}</td>'
            "</tr>"
        )
    return "".join(parts)


def _render_recent_rows(recent: list[dict]) -> str:
    if not recent:
        return '<tr><td colspan="5" class="jobs-empty">No runs in window.</td></tr>'
    parts: list[str] = []
    for r in recent:
        status = (r.get("status") or "unknown").lower()
        if status not in ("success", "failed", "running", "retrying", "unknown"):
            status = "unknown"
        err = r.get("error_message") or ""
        err_html = (
            f'<span class="jobs-cell-error">{_esc(str(err)[:200])}</span>'
            if err else ""
        )
        finished = r.get("finished_at") or r.get("started_at")
        parts.append(
            "<tr>"
            f'<td><span class="jobs-cell-name">{_esc(r.get("job_name"))}</span></td>'
            f'<td><span class="jobs-status jobs-status--{status}">{_esc(status)}</span></td>'
            f'<td class="num">{_esc(_fmt_duration(r.get("duration_ms")))}</td>'
            f'<td class="jobs-cell-mono">{_esc(_fmt_ts(finished))}</td>'
            f"<td>{err_html}</td>"
            "</tr>"
        )
    return "".join(parts)


def _render_filter_options(names: list[str]) -> str:
    """Render the ``<option>`` list for the job-name filter dropdown.

    The template's ``raw_filter_options`` slot lives inside
    ``<select id="jobs-filter-name">...</select>``. We keep that slot's
    contract narrow so it remains a list of options.
    """
    return "".join(
        f'<option value="{_esc(n)}">{_esc(n)}</option>' for n in names
    )


def _render_filter_mount_script(
    *,
    status_options_html: str,
    filter_q: str,
    filter_since: str,
    filter_until: str,
) -> str:
    """Build the JS that mounts the full filter form on page load.

    The template's filter region is intentionally minimal (a single
    job-name ``<select>``). To keep the file-level boundary with the
    template clean, the rest of the filter UI (status, free-text search,
    date range) is mounted DOM-side from this script: it locates the
    ``.jobs-card__filters`` div inside the Recent-runs card and replaces
    it with a full GET form pre-populated from the server-side filter
    state. The form submits to ``/admin/jobs`` so the URL carries the
    active filter set.

    All values that come from user input (``filter_q``, dates) are
    escaped here as HTML attribute values; the status options are
    pre-rendered by ``_render_status_options`` against the
    ``_JOB_STATUS_LABELS`` whitelist.

    The script is wrapped so it can be appended to the existing
    ``raw_filter_options`` slot — the slot lives inside a ``<select>``,
    so we close that early then emit the script as a sibling.
    """
    import json as _json
    q_attr = _esc(filter_q)
    since_attr = _esc(filter_since)
    until_attr = _esc(filter_until)
    # JSON-encode the option markup so the JS embedding is unambiguous.
    status_options_json = _json.dumps(status_options_html)
    # The browser parser closes the surrounding ``<select>`` when it sees
    # our ``</select>`` here; the literal ``</select>`` later in the
    # template is then a stray tag that browsers tolerate. The injected
    # ``<script>`` runs at parse time, so the rest of the page (and its
    # poll-loop script) hasn't bound to the old dropdown yet — we rebuild
    # the DOM before the existing script does, and the existing script's
    # ``document.getElementById('jobs-filter-name')`` lookup transparently
    # finds the new ``<select>`` because we keep the same id.
    return (
        "</select>"
        "<script>"
        "(function(){"
        "var STATUS_OPTS=" + status_options_json + ";"
        "var Q=" + _json.dumps(filter_q) + ";"
        "var SINCE=" + _json.dumps(filter_since) + ";"
        "var UNTIL=" + _json.dumps(filter_until) + ";"
        "function esc(s){return String(s==null?'':s).replace(/[&<>\"']/g,"
        "function(c){return ({'&':'&amp;','<':'&lt;','>':'&gt;',"
        "'\"':'&quot;','\\'':'&#39;'})[c];});}"
        "function mount(){"
        "var host=document.querySelector('.jobs-card__filters');"
        "if(!host) return;"
        "var oldSelect=document.getElementById('jobs-filter-name');"
        "var oldOptions=oldSelect?oldSelect.innerHTML:'';"
        "var form=document.createElement('form');"
        "form.className='jobs-filter';"
        "form.setAttribute('method','get');"
        "form.setAttribute('action','/admin/jobs');"
        "form.setAttribute('aria-label','Filter recent runs');"
        "form.id='jobs-filter-form';"
        "form.innerHTML="
        "'<label class=\"jobs-filter__field\">'"
        "+'<span class=\"jobs-filter__label\">Search</span>'"
        "+'<input class=\"jobs-filter__input\" type=\"search\" name=\"q\" id=\"jobs-filter-q\" autocomplete=\"off\" placeholder=\"job name contains\\u2026\" value=\"'+esc(Q)+'\">'"
        "+'</label>'"
        "+'<label class=\"jobs-filter__field\">'"
        "+'<span class=\"jobs-filter__label\">Job</span>'"
        "+'<select class=\"jobs-filter__input\" name=\"job_name\" id=\"jobs-filter-name\">'+oldOptions+'</select>'"
        "+'</label>'"
        "+'<label class=\"jobs-filter__field\">'"
        "+'<span class=\"jobs-filter__label\">Status</span>'"
        "+'<select class=\"jobs-filter__input\" name=\"status\" id=\"jobs-filter-status\">'+STATUS_OPTS+'</select>'"
        "+'</label>'"
        "+'<label class=\"jobs-filter__field jobs-filter__field--date\">'"
        "+'<span class=\"jobs-filter__label\">Since</span>'"
        "+'<input class=\"jobs-filter__input\" type=\"date\" name=\"since\" id=\"jobs-filter-since\" value=\"'+esc(SINCE)+'\">'"
        "+'</label>'"
        "+'<label class=\"jobs-filter__field jobs-filter__field--date\">'"
        "+'<span class=\"jobs-filter__label\">Until</span>'"
        "+'<input class=\"jobs-filter__input\" type=\"date\" name=\"until\" id=\"jobs-filter-until\" value=\"'+esc(UNTIL)+'\">'"
        "+'</label>'"
        "+'<div class=\"jobs-filter__actions\">'"
        "+'<button class=\"jobs-filter__submit\" type=\"submit\">Apply</button>'"
        "+'<a class=\"jobs-filter__clear\" href=\"/admin/jobs\">Clear</a>'"
        "+'</div>';"
        "var card=host.closest('.jobs-card__head');"
        "if(card&&card.parentNode){card.parentNode.insertBefore(form,card.nextSibling);host.remove();}"
        "else{host.replaceWith(form);}"
        "}"
        "function injectStyles(){"
        "if(document.getElementById('jobs-filter-injected-styles'))return;"
        "var s=document.createElement('style');"
        "s.id='jobs-filter-injected-styles';"
        "s.textContent="
        "'.jobs-filter{display:grid;grid-template-columns:minmax(180px,1.6fr) minmax(140px,1.2fr) minmax(120px,1fr) minmax(120px,1fr) minmax(120px,1fr) auto;gap:var(--space-3,12px);align-items:end;margin-bottom:var(--space-4,16px);padding-bottom:var(--space-4,16px);border-bottom:1px solid var(--border-ghost,var(--border-default))}'"
        "+'.jobs-filter__field{display:flex;flex-direction:column;gap:4px;min-width:0}'"
        "+'.jobs-filter .jobs-filter__label{font-family:var(--font-ui);font-size:11px;font-weight:500;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.06em}'"
        "+'.jobs-filter__input{font-family:var(--font-ui);font-size:13px;padding:6px 10px;border:1px solid var(--border-default,var(--border-strong));background:var(--bg-base);color:var(--text-primary);border-radius:var(--radius-sm,4px);width:100%}'"
        "+'select.jobs-filter__input{cursor:pointer}'"
        "+'.jobs-filter__input:focus{outline:2px solid var(--text-primary);outline-offset:2px}'"
        "+'.jobs-filter__actions{display:flex;align-items:center;gap:var(--space-2,8px)}'"
        "+'.jobs-filter__submit{font-family:var(--font-ui);font-size:12px;font-weight:500;padding:7px 14px;border:1px solid var(--text-primary);background:var(--text-primary);color:var(--bg-base);border-radius:var(--radius-sm,4px);cursor:pointer}'"
        "+'.jobs-filter__submit:hover{opacity:0.85}'"
        "+'.jobs-filter__clear{font-family:var(--font-ui);font-size:12px;color:var(--text-tertiary);text-decoration:none;padding:7px 4px}'"
        "+'.jobs-filter__clear:hover{color:var(--text-primary);text-decoration:underline}'"
        "+'@media (max-width:900px){.jobs-filter{grid-template-columns:1fr 1fr}.jobs-filter__actions{grid-column:1 / -1;justify-content:flex-end}}';"
        "document.head.appendChild(s);"
        "}"
        "if(document.readyState==='loading'){"
        "document.addEventListener('DOMContentLoaded',function(){injectStyles();mount();});"
        "}else{injectStyles();mount();}"
        "})();"
        "</script>"
    )


def _parse_date_to_ts(s) -> Optional[int]:
    """Parse ``YYYY-MM-DD`` into unix seconds (start-of-day UTC).

    Mirrors ``admin_routes._parse_date_to_ts`` — bad input silently
    disables the filter rather than 400ing, so a fat-fingered date in
    the URL doesn't break the page.
    """
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return int(
            _dt.datetime.strptime(s, "%Y-%m-%d")
            .replace(tzinfo=_dt.timezone.utc)
            .timestamp()
        )
    except (TypeError, ValueError):
        return None


def _render_status_options(active: str) -> str:
    """Build the <option> list for the status-filter dropdown.

    All values come from the ``_JOB_STATUS_LABELS`` whitelist, so the
    ``html.escape`` calls below are defence-in-depth rather than the
    primary safety boundary.
    """
    out = ['<option value="">Any status</option>']
    for label in _JOB_STATUS_LABELS:
        sel = " selected" if active == label else ""
        out.append(
            f'<option value="{_esc(label)}"{sel}>{_esc(label)}</option>'
        )
    return "".join(out)


def _apply_recent_filters(
    rows: list[dict],
    *,
    status: str,
    q: str,
    since_ts: Optional[int],
    until_ts: Optional[int],
) -> list[dict]:
    """In-memory filter over the recent-runs list.

    The underlying query (``list_recent_job_runs``) only knows how to
    filter by ``job_name``; everything else is applied here. The list
    is bounded to 100 rows so the O(n) pass is trivial. ``status``,
    ``q``, and the date range come from validated/whitelisted querystring
    inputs in the caller.
    """
    if not rows:
        return rows
    out = rows
    if status:
        out = [r for r in out if (r.get("status") or "").lower() == status]
    if q:
        needle = q.lower()
        out = [
            r for r in out
            if needle in (r.get("job_name") or "").lower()
        ]
    if since_ts is not None:
        out = [
            r for r in out
            if int(r.get("started_at") or 0) >= since_ts
        ]
    if until_ts is not None:
        out = [
            r for r in out
            if int(r.get("started_at") or 0) <= until_ts
        ]
    return out


# ── JSON: live snapshot for the 5s poll ──────────────────────────────────

@server.app.get("/admin/api/jobs/refresh")
@rate_limit(limit=300, window_seconds=60, key_func=_admin_key)
async def admin_api_jobs_refresh(request: Request, job_name: Optional[str] = None) -> JSONResponse:
    """Return everything the page needs in one round trip."""
    user = server._require_admin_user(request)
    if not isinstance(user, dict):  # pragma: no cover — defensive
        raise HTTPException(status_code=403, detail="Admin required")
    stats = job_queries.get_job_stats(window_hours=24)
    running = job_queries.list_currently_running(limit=50)
    cron = job_queries.list_cron_schedule()
    recent = job_queries.list_recent_job_runs(limit=100, job_name=job_name or None)
    return JSONResponse({
        "stats": stats,
        "running": running,
        "cron": cron,
        "recent": recent,
        "generated_at": int(time.time()),
    })


# ── JSON API — preserved from the previous shape for back-compat ─────────

@server.app.get("/admin/api/jobs")
@rate_limit(limit=120, window_seconds=60, key_func=_admin_key)
async def admin_api_jobs(request: Request) -> JSONResponse:
    server._require_admin_user(request)
    try:
        from scheduler import scheduler as sched
        metadata = sched.jobs_metadata()
    except Exception:
        log.exception("admin_jobs: scheduler metadata failed")
        metadata = []
    stats = _job_stats()
    out = []
    for meta in metadata:
        name = meta["name"]
        s = stats.get(name, {})
        out.append({
            **meta,
            "last_run": s.get("last_run"),
            "last_ok": s.get("last_ok"),
            "last_duration_ms": s.get("last_duration_ms"),
            "avg_ms": s.get("avg_ms"),
            "fail_count_24h": s.get("fail_count_24h") or 0,
            "total_runs": s.get("total_runs") or 0,
        })
    return JSONResponse({"jobs": out, "count": len(out)})


@server.app.get("/admin/api/jobs/{name}/history")
@rate_limit(limit=60, window_seconds=60, key_func=_admin_key)
async def admin_api_job_history(request: Request, name: str) -> JSONResponse:
    server._require_admin_user(request)
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, started_at, completed_at, duration_ms, ok, error, triggered_by "
            "FROM job_runs WHERE job_name = ? "
            "ORDER BY started_at DESC LIMIT 50",
            (name,),
        ).fetchall()
    return JSONResponse({"runs": [dict(r) for r in rows], "count": len(rows)})


@server.app.post("/admin/api/jobs/{name}/pause")
@rate_limit(limit=30, window_seconds=60, key_func=_admin_key)
async def admin_api_job_pause(request: Request, name: str) -> JSONResponse:
    server._require_admin_user(request)
    from scheduler import scheduler as sched
    try:
        sched.pause(name)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"pause failed: {exc}")
    return JSONResponse({"ok": True, "paused": name})


@server.app.post("/admin/api/jobs/{name}/resume")
@rate_limit(limit=30, window_seconds=60, key_func=_admin_key)
async def admin_api_job_resume(request: Request, name: str) -> JSONResponse:
    server._require_admin_user(request)
    from scheduler import scheduler as sched
    try:
        sched.resume(name)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"resume failed: {exc}")
    return JSONResponse({"ok": True, "resumed": name})


@server.app.post("/admin/api/jobs/{name}/trigger")
@rate_limit(limit=30, window_seconds=60, key_func=_admin_key)
async def admin_api_job_trigger(request: Request, name: str) -> JSONResponse:
    server._require_admin_user(request)
    from scheduler import scheduler as sched
    try:
        sched.trigger_now(name, triggered_by="admin")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"trigger failed: {exc}")
    return JSONResponse({"ok": True, "triggered": name})


# ── HTML page ────────────────────────────────────────────────────────────

@server.app.get("/admin/jobs", response_class=HTMLResponse)
async def admin_jobs_page(request: Request):
    """Render the /admin/jobs dashboard inside the admin shell.

    Recent-runs filters (querystring, all optional):
      * ``status``  — one of ``running`` / ``success`` / ``failed`` /
                      ``scheduled``. Anything else is silently dropped.
      * ``q``       — substring match against ``job_name`` (case-insensitive).
      * ``since`` / ``until`` — ``YYYY-MM-DD`` bounds on ``started_at``.
                                Malformed dates are silently dropped so a
                                bookmarked URL never 400s.

    Mirrors the validation discipline of ``admin_routes.email_addresses_page``:
    whitelist every value, treat bad input as "no filter" rather than an
    error, and round-trip the parsed values back into the form so the
    user sees what's active.
    """
    user = server._require_admin_user(request, page=True)
    if user is None:
        return server._denied_response(request)
    if not isinstance(user, dict):
        return user  # RedirectResponse for 2FA

    qp = request.query_params
    status_filter = (qp.get("status") or "").strip().lower()
    q = (qp.get("q") or "").strip()
    since_str = (qp.get("since") or "").strip()
    until_str = (qp.get("until") or "").strip()

    if status_filter and status_filter not in _JOB_STATUS_LABELS:
        status_filter = ""
    # Bound the free-text needle so a pathological querystring can't bloat
    # the filter loop. 200 chars is well past any legitimate job name.
    if len(q) > 200:
        q = q[:200]

    since_ts = _parse_date_to_ts(since_str)
    until_ts = _parse_date_to_ts(until_str)
    if until_ts is not None:
        # Treat until-date as inclusive end-of-day, same convention as
        # the email-addresses page.
        until_ts += 86_399

    # Snapshot for the initial paint. Polling JS upgrades from here.
    try:
        stats = job_queries.get_job_stats(window_hours=24)
        running = job_queries.list_currently_running(limit=50)
        cron = job_queries.list_cron_schedule()
        recent = job_queries.list_recent_job_runs(limit=100)
        names = job_queries.list_distinct_job_names()
    except Exception:
        log.exception("admin_jobs_page: initial snapshot failed")
        stats = {"total_runs": 0, "success_count": 0, "failed_count": 0,
                 "success_rate": None, "avg_duration_ms": None, "window_hours": 24}
        running, cron, recent, names = [], [], [], []

    recent = _apply_recent_filters(
        recent,
        status=status_filter,
        q=q,
        since_ts=since_ts,
        until_ts=until_ts,
    )

    # The Recent-runs card in admin/jobs.html only exposes a single
    # ``raw_filter_options`` slot (inside a ``<select>``). We extend that
    # slot to carry the full filter form: it emits the job-name options
    # the slot was originally designed for, closes the surrounding
    # ``<select>`` early, and then injects a small bootstrap script that
    # mounts the rest of the form (status, search, date range) DOM-side
    # on page load. See ``_render_filter_mount_script`` for the rationale.
    filter_slot_html = (
        _render_filter_options(names)
        + _render_filter_mount_script(
            status_options_html=_render_status_options(status_filter),
            filter_q=q,
            filter_since=since_str if since_ts is not None else "",
            filter_until=until_str if until_ts is not None else "",
        )
    )

    return render_admin_page(
        request,
        "admin/jobs.html",
        page_title="Background jobs",
        active_route="jobs",
        breadcrumb=[("Admin", "/admin"), ("Jobs", "/admin/jobs")],
        raw_stat_total=str(stats.get("total_runs") or 0),
        raw_stat_rate=_fmt_pct(stats.get("success_rate")),
        raw_stat_avg=_fmt_duration(stats.get("avg_duration_ms")),
        raw_stat_failed=str(stats.get("failed_count") or 0),
        raw_running_count=str(len(running)),
        raw_running_rows=_render_running_rows(running),
        raw_cron_count=str(len(cron)),
        raw_cron_rows=_render_cron_rows(cron),
        raw_recent_rows=_render_recent_rows(recent),
        raw_filter_options=filter_slot_html,
    )
