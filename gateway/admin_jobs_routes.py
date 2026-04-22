"""Admin /admin/jobs page + pause/resume/trigger APIs.

Registered by being imported at the bottom of ``server.py`` (same pattern
as ``notification_routes``, ``push_routes``, etc.).

Routes exposed:
    GET  /admin/jobs                       HTML page
    GET  /admin/api/jobs                   JSON list (one row per job)
    GET  /admin/api/jobs/{name}/history    last 50 runs for a job
    POST /admin/api/jobs/{name}/pause      pause schedule
    POST /admin/api/jobs/{name}/resume     resume schedule
    POST /admin/api/jobs/{name}/trigger    fire now (records triggered_by=admin)

Every route goes through ``server._require_admin_user``. POSTs enforce
the global CSRF middleware — no exemption. Rate limits are generous
(the admin panel polls every 5s).
"""

from __future__ import annotations

import datetime as _dt
import html
import logging
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import db
import server
from security.rate_limiter import rate_limit, get_client_ip

log = logging.getLogger("admin_jobs")


def _admin_key(request: Request) -> str:
    user = server.current_user(request)
    if user and user.get("is_admin"):
        return f"admin_jobs:{user['user_id']}"
    return f"admin_jobs:anon:{get_client_ip(request)}"


# ── Aggregated per-job stats from job_runs ───────────────────────────────

def _job_stats() -> dict[str, dict]:
    """Summarise job_runs for every registered job.

    Returns ``{job_name: {last_run, last_ok, last_duration_ms, avg_ms,
    fail_count_24h, total_runs}}``. Runs only against ``job_runs``
    (migration 105). If the table doesn't exist yet, returns empty.
    """
    import time
    now = int(time.time())
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


# ── JSON API ─────────────────────────────────────────────────────────────

@server.app.get("/admin/api/jobs")
@rate_limit(limit=120, window_seconds=60, key_func=_admin_key)
async def admin_api_jobs(request: Request) -> JSONResponse:
    server._require_admin_user(request)
    from scheduler import scheduler as sched
    stats = _job_stats()
    out = []
    for meta in sched.jobs_metadata():
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

_JOBS_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Jobs — Admin — narve.ai</title>
  <link rel="stylesheet" href="/_gateway_static/gateway.css?v=7">
  <style>
    .jobs-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .jobs-table th, .jobs-table td {
      padding: 10px 8px; text-align: left;
      border-bottom: 1px solid var(--border-subtle);
      vertical-align: top;
    }
    .jobs-table th {
      font-weight: 600; color: var(--text-tertiary);
      font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
    }
    .jobs-table tr[data-paused="1"] td { opacity: 0.55; }
    .jobs-table tr[data-failing="1"] td { background: rgba(239, 68, 68, 0.06); }
    .jobs-status-ok { color: var(--green, #15803d); }
    .jobs-status-fail { color: var(--red, #dc2626); font-weight: 600; }
    .jobs-status-pending { color: var(--text-tertiary); }
    .jobs-btn {
      padding: 4px 10px; font-size: 11px;
      background: transparent;
      border: 1px solid var(--border-default);
      border-radius: 4px; cursor: pointer;
      color: var(--text-primary);
      font-family: inherit;
      margin-right: 4px;
    }
    .jobs-btn:hover { background: var(--interactive-ghost); }
    .jobs-btn[disabled] { opacity: 0.4; cursor: not-allowed; }
    .jobs-trigger { border-color: var(--text-primary); }
    .jobs-name { font-family: var(--font-mono); font-size: 12px; }
    .jobs-trigger-expr {
      font-family: var(--font-mono); font-size: 11px;
      color: var(--text-tertiary); margin-top: 2px;
    }
    .jobs-history {
      margin: 8px 0 16px; padding: 10px 12px;
      background: var(--bg-inset); border-radius: 6px;
      font-size: 11px;
    }
    .jobs-history td { padding: 2px 8px 2px 0; }
    .jobs-empty { color: var(--text-tertiary); padding: 24px; text-align: center; }
    .jobs-failing-summary {
      padding: 10px 14px; margin-bottom: 16px;
      background: rgba(239, 68, 68, 0.08);
      border: 1px solid rgba(239, 68, 68, 0.2);
      border-radius: 6px; font-size: 13px;
    }
  </style>
</head>
<body>
  <div class="app-shell">
    {{ raw_admin_sidebar }}
    <main class="main-content" id="main" tabindex="-1">
      <div class="breadcrumb">
        <a href="/admin" class="breadcrumb-item" style="text-decoration:none;color:inherit">Admin</a>
        <span class="breadcrumb-separator">/</span>
        <span class="breadcrumb-item current">Jobs</span>
      </div>
      <div class="page-header">
        <h1 class="page-title">Scheduled jobs</h1>
        <p class="page-subtitle">Every recurring job narve.ai runs, sourced from <code>scheduler.registry</code>. Rows with recent failures are highlighted.</p>
      </div>
      <div id="failing-summary" class="jobs-failing-summary" style="display:none"></div>
      <div class="content-area">
        <table class="jobs-table">
          <thead>
            <tr>
              <th>Job</th>
              <th>Trigger</th>
              <th>Next run</th>
              <th>Last run</th>
              <th>Duration</th>
              <th>24h fails</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="jobs-body">
            <tr><td colspan="7" class="jobs-empty">Loading…</td></tr>
          </tbody>
        </table>
      </div>
    </main>
  </div>
<script>
(function(){
  function csrf(){var m=document.cookie.match(/(?:^|;\\s*)_csrf=([^;]+)/);return m?decodeURIComponent(m[1]):"";}
  function fmtTs(t){if(!t)return "—";var d=new Date(t*1000);var now=Date.now();var delta=(now-d.getTime())/1000;if(delta<0){var future=Math.abs(delta);if(future<60)return "in "+Math.round(future)+"s";if(future<3600)return "in "+Math.round(future/60)+"m";if(future<86400)return "in "+Math.round(future/3600)+"h";return d.toLocaleString();}if(delta<60)return Math.round(delta)+"s ago";if(delta<3600)return Math.round(delta/60)+"m ago";if(delta<86400)return Math.round(delta/3600)+"h ago";return d.toLocaleDateString();}
  function fmtDur(ms){if(ms==null)return "—";if(ms<1000)return ms+"ms";if(ms<60000)return (ms/1000).toFixed(1)+"s";return Math.round(ms/1000)+"s";}
  function fmtStatus(ok){if(ok==null)return '<span class="jobs-status-pending">—</span>';if(ok==1)return '<span class="jobs-status-ok">✓</span>';return '<span class="jobs-status-fail">✗</span>';}

  function action(name, verb){
    return fetch('/admin/api/jobs/'+encodeURIComponent(name)+'/'+verb, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'X-CSRF-Token': csrf(), 'Content-Type': 'application/json'},
    }).then(function(r){ if(!r.ok) throw new Error('status '+r.status); return load(); });
  }

  function toggleHistory(name, row){
    var existing = row.nextElementSibling;
    if (existing && existing.classList.contains('jobs-history-row')) { existing.remove(); return; }
    fetch('/admin/api/jobs/'+encodeURIComponent(name)+'/history', {credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(data){
        var tr = document.createElement('tr');
        tr.className = 'jobs-history-row';
        var runs = data.runs || [];
        var html;
        if (runs.length === 0) {
          html = '<div class="jobs-history">No runs yet.</div>';
        } else {
          var rows = runs.map(function(r){
            var status = fmtStatus(r.ok);
            var err = r.error ? ' — <span style="color:var(--red,#dc2626)">'+escape(r.error)+'</span>' : '';
            var who = r.triggered_by === 'admin' ? ' <em>(manual)</em>' : '';
            return '<tr><td>'+status+'</td><td>'+fmtTs(r.started_at)+who+'</td><td>'+fmtDur(r.duration_ms)+'</td>'+(r.error ? '<td style="word-break:break-word">'+escape(r.error.slice(0,120))+'</td>':'<td></td>')+'</tr>';
          }).join('');
          html = '<div class="jobs-history"><table><tbody>'+rows+'</tbody></table></div>';
        }
        tr.innerHTML = '<td colspan="7">'+html+'</td>';
        row.insertAdjacentElement('afterend', tr);
      });
  }

  function escape(s){return String(s||'').replace(/[&<>"']/g,function(c){return({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]);});}

  function load(){
    return fetch('/admin/api/jobs', {credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(data){
        var tbody = document.getElementById('jobs-body');
        tbody.innerHTML = '';
        var failing = (data.jobs || []).filter(function(j){ return (j.fail_count_24h||0) > 0; });
        var fs = document.getElementById('failing-summary');
        if (failing.length) {
          fs.textContent = failing.length + ' job' + (failing.length === 1 ? '' : 's') + ' had failures in the last 24h: ' + failing.map(function(j){return j.name;}).join(', ');
          fs.style.display = '';
        } else { fs.style.display = 'none'; }
        if (!data.jobs || data.jobs.length === 0) {
          tbody.innerHTML = '<tr><td colspan="7" class="jobs-empty">No jobs registered. Scheduler may be disabled.</td></tr>';
          return;
        }
        data.jobs.forEach(function(j){
          var tr = document.createElement('tr');
          if (j.paused) tr.setAttribute('data-paused', '1');
          if ((j.fail_count_24h||0) > 0) tr.setAttribute('data-failing', '1');
          tr.innerHTML =
            '<td><div class="jobs-name">'+escape(j.name)+'</div><div class="jobs-trigger-expr">'+escape(j.func_module+'.'+j.func_name)+'</div></td>'+
            '<td class="jobs-trigger-expr">'+escape(j.trigger||'')+'</td>'+
            '<td>'+fmtTs(j.next_run_time)+'</td>'+
            '<td>'+fmtStatus(j.last_ok)+' '+fmtTs(j.last_run)+'</td>'+
            '<td>'+fmtDur(j.last_duration_ms)+(j.avg_ms ? ' <span style="color:var(--text-tertiary)">avg '+fmtDur(j.avg_ms)+'</span>' : '')+'</td>'+
            '<td>'+(j.fail_count_24h||0)+'</td>'+
            '<td></td>';
          var cell = tr.cells[6];
          var mkBtn = function(label, verb){
            var b = document.createElement('button');
            b.className = 'jobs-btn' + (verb==='trigger' ? ' jobs-trigger' : '');
            b.textContent = label;
            b.addEventListener('click', function(e){ e.stopPropagation(); action(j.name, verb); });
            cell.appendChild(b);
          };
          mkBtn(j.paused ? 'Resume' : 'Pause', j.paused ? 'resume' : 'pause');
          mkBtn('Trigger', 'trigger');
          tr.addEventListener('click', function(){ toggleHistory(j.name, tr); });
          tr.style.cursor = 'pointer';
          tbody.appendChild(tr);
        });
      })
      .catch(function(err){
        var tbody = document.getElementById('jobs-body');
        tbody.innerHTML = '<tr><td colspan="7" class="jobs-empty">Failed to load: '+escape(err.message)+'</td></tr>';
      });
  }

  load();
  setInterval(load, 10000);
})();
</script>
</body>
</html>"""


@server.app.get("/admin/jobs", response_class=HTMLResponse)
async def admin_jobs_page(request: Request) -> HTMLResponse:
    admin_or_redirect = server._require_admin_user(request, page=True)
    if admin_or_redirect is None:
        raise HTTPException(status_code=403, detail="Admin required")
    if not isinstance(admin_or_redirect, dict):
        # _require_admin_user returned a RedirectResponse during 2FA (now
        # removed, but the signature still allows it). Pass it through.
        return admin_or_redirect  # type: ignore[return-value]

    admin = admin_or_redirect
    # Reuse the existing admin sidebar rendering if it exists; otherwise
    # inject a minimal link back to /admin.
    try:
        sidebar = server._admin_sidebar_html(admin)  # type: ignore[attr-defined]
    except Exception:
        sidebar = (
            '<aside class="sidebar">'
            '<div class="sidebar-logo"><span class="sidebar-logo-text">narve.ai admin</span></div>'
            '<nav class="sidebar-nav">'
            '<a href="/admin" class="nav-item">← Admin home</a>'
            '<a href="/admin/jobs" class="nav-item active">Jobs</a>'
            '</nav>'
            '</aside>'
        )
    page_html = _JOBS_PAGE.replace("{{ raw_admin_sidebar }}", sidebar)
    return HTMLResponse(page_html)
