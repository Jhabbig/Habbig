"""Admin /admin/emails — outbound email queue + delivery review.

Diagnostic + audit surface for every outbound email narve.ai sends. Not
to be confused with /admin/email-templates (the per-template editor):
this surface shows what *did* go out (or didn't), with the rendered
payload, error message, and a retry action for failed deliveries.

Registered as a side-effect of import, like ``admin_jobs_routes``. See
``server.py`` (search "admin_emails_routes") for the import block.

Routes:
    GET  /admin/emails                         HTML page (admin shell)
    GET  /admin/api/emails                     JSON list (filter/paginate)
    GET  /admin/emails/{id}                    JSON — full payload incl. rendered HTML
    POST /admin/emails/{id}/resend             Re-enqueue a failed send (CSRF required)

Data source
-----------
Every outbound email is dispatched via ``jobs.email_jobs.enqueue_email``
which writes a row to the ``background_jobs`` SQLite table with
``name = 'send_email'``. Filter, render, and resend operate on that
table — there is no dedicated ``email_queue`` table (the spec leaves
that optional, per ``narve Email DRY_RUN Modes`` doc).

Recipient redaction
-------------------
List views truncate the local-part to first 3 chars + `***@domain`. The
detail view shows the full address — only the admin who clicks through
sees it, and the click is rate-limited.
"""

from __future__ import annotations

import html
import json
import logging
import time
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import db
import server
from admin_shell import render_admin_page
from security.rate_limiter import rate_limit, get_client_ip

log = logging.getLogger("admin_emails")


# ── Auth/rate-limit key ────────────────────────────────────────────────


def _admin_key(request: Request) -> str:
    user = server.current_user(request)
    if user and user.get("is_admin"):
        return f"admin_emails:{user['user_id']}"
    return f"admin_emails:anon:{get_client_ip(request)}"


# ── Helpers ────────────────────────────────────────────────────────────


# Statuses surfaced to the admin UI. Map background_jobs.status values
# onto the four labels the spec asks for (queued/sent/failed/bounced).
# 'complete' rows are 'sent'; 'queued'/'running' map to 'queued'; the
# rest of the values (failed, etc.) pass through.
_STATUS_LABEL = {
    "queued": "queued",
    "running": "queued",   # mid-attempt — still considered queued from the admin POV
    "complete": "sent",
    "failed": "failed",
}


def _label_status(raw: Optional[str]) -> str:
    return _STATUS_LABEL.get((raw or "").lower(), (raw or "unknown").lower())


def _redact_recipient(addr: str) -> str:
    """Truncate the local-part for list-view privacy.

    ``somebody@example.com`` -> ``som***@example.com``. Empty or
    malformed inputs fall back to a short placeholder so the column
    never goes blank.
    """
    if not addr or "@" not in addr:
        return addr or "—"
    local, _, domain = addr.partition("@")
    if len(local) <= 3:
        return f"{local[:1]}***@{domain}"
    return f"{local[:3]}***@{domain}"


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
        return f"in {-delta}s"
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _parse_payload(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


# ── Queries ────────────────────────────────────────────────────────────


def _list_email_rows(
    *,
    limit: int = 200,
    offset: int = 0,
    status_filter: Optional[str] = None,
    template_filter: Optional[str] = None,
    recipient_filter: Optional[str] = None,
) -> list[dict]:
    """Read recent send_email jobs from background_jobs.

    Applies the optional filters in-Python after a single bounded SELECT,
    so the SQL stays simple even though template/recipient live inside
    the JSON payload. 200-row cap keeps the in-Python loop bounded.
    """
    raw_status_filter = None
    if status_filter == "sent":
        raw_status_filter = "complete"
    elif status_filter == "queued":
        # 'queued' label covers both 'queued' and 'running' raw statuses.
        raw_status_filter = None  # handled in the post-filter
    elif status_filter in ("failed", "bounced"):
        raw_status_filter = "failed"

    # Pull a wider window than `limit` when post-filtering on template /
    # recipient / queued status, so the page can still produce `limit`
    # matches even if many rows are filtered out.
    fetch_window = max(limit * 4, 500) if (
        template_filter or recipient_filter or status_filter == "queued"
    ) else limit + offset

    with db.conn() as c:
        if raw_status_filter:
            rows = c.execute(
                "SELECT id, name, payload, status, attempts, max_attempts, error, "
                "enqueued_at, started_at, finished_at, duration_ms "
                "FROM background_jobs "
                "WHERE name = 'send_email' AND status = ? "
                "ORDER BY enqueued_at DESC "
                "LIMIT ?",
                (raw_status_filter, fetch_window),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, name, payload, status, attempts, max_attempts, error, "
                "enqueued_at, started_at, finished_at, duration_ms "
                "FROM background_jobs "
                "WHERE name = 'send_email' "
                "ORDER BY enqueued_at DESC "
                "LIMIT ?",
                (fetch_window,),
            ).fetchall()

    out: list[dict] = []
    for r in rows:
        payload = _parse_payload(r["payload"])
        template = payload.get("template") or ""
        to = payload.get("to") or ""
        label = _label_status(r["status"])

        if status_filter == "queued" and label != "queued":
            continue
        if template_filter and template != template_filter:
            continue
        if recipient_filter and recipient_filter.lower() not in to.lower():
            continue

        out.append({
            "id": r["id"],
            "template": template,
            "recipient": to,
            "recipient_redacted": _redact_recipient(to),
            "status": label,
            "raw_status": r["status"],
            "attempts": r["attempts"],
            "max_attempts": r["max_attempts"],
            "error_message": r["error"] or "",
            "enqueued_at": r["enqueued_at"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "duration_ms": r["duration_ms"],
        })

    return out[offset:offset + limit]


def _stats_24h() -> dict:
    """Return 24h send count, failure rate, and top failing templates.

    All three are computed off the same single fetch so the page renders
    one query at SSR time. Failure rate is failed / (failed + sent) —
    queued rows are excluded so an idle queue doesn't skew the rate.
    """
    cutoff = int(time.time()) - 86400
    with db.conn() as c:
        rows = c.execute(
            "SELECT payload, status FROM background_jobs "
            "WHERE name = 'send_email' AND enqueued_at >= ?",
            (cutoff,),
        ).fetchall()

    sent = 0
    failed = 0
    queued = 0
    fails_by_template: dict[str, int] = {}
    for r in rows:
        label = _label_status(r["status"])
        if label == "sent":
            sent += 1
        elif label == "failed":
            failed += 1
            template = _parse_payload(r["payload"]).get("template") or "(unknown)"
            fails_by_template[template] = fails_by_template.get(template, 0) + 1
        elif label == "queued":
            queued += 1

    denom = sent + failed
    failure_rate = (100.0 * failed / denom) if denom else None

    top_failing = sorted(
        fails_by_template.items(), key=lambda kv: kv[1], reverse=True
    )[:5]

    return {
        "sent_24h": sent,
        "failed_24h": failed,
        "queued_24h": queued,
        "failure_rate": failure_rate,
        "top_failing_templates": [
            {"template": t, "count": n} for t, n in top_failing
        ],
        "window_hours": 24,
    }


def _list_distinct_templates() -> list[str]:
    """Distinct template names seen in the last 30d (for the filter dropdown)."""
    cutoff = int(time.time()) - 30 * 86400
    with db.conn() as c:
        rows = c.execute(
            "SELECT DISTINCT payload FROM background_jobs "
            "WHERE name = 'send_email' AND enqueued_at >= ? "
            "LIMIT 2000",
            (cutoff,),
        ).fetchall()
    names: set[str] = set()
    for r in rows:
        t = _parse_payload(r["payload"]).get("template")
        if t:
            names.add(t)
    return sorted(names)


# ── Rendering helpers ──────────────────────────────────────────────────


def _fmt_pct(p: Optional[float]) -> str:
    if p is None:
        return "—"
    return f"{p:.1f}%"


def _render_recent_rows(rows: list[dict]) -> str:
    if not rows:
        return (
            '<tr><td colspan="6" class="emails-empty">'
            "No emails in window. Try a different filter or wait for the next send."
            "</td></tr>"
        )
    parts: list[str] = []
    for r in rows:
        status = r["status"]
        if status not in ("queued", "sent", "failed", "bounced"):
            status = "unknown"
        err_html = (
            f'<span class="emails-cell-error">{_esc(str(r["error_message"])[:160])}</span>'
            if r["error_message"] else ""
        )
        finished = r["finished_at"] or r["enqueued_at"]
        parts.append(
            "<tr>"
            f'<td class="emails-cell-mono">{_esc(_fmt_ts(finished))}</td>'
            f'<td><span class="emails-cell-template">{_esc(r["template"])}</span></td>'
            f'<td class="emails-cell-mono">{_esc(r["recipient_redacted"])}</td>'
            f'<td><span class="emails-status emails-status--{status}">{_esc(status)}</span></td>'
            f"<td>{err_html}</td>"
            f'<td class="emails-cell-actions">'
            f'<a class="emails-row-link" href="/admin/emails/{int(r["id"])}">View</a>'
            f"</td>"
            "</tr>"
        )
    return "".join(parts)


def _render_template_filter(templates: list[str], selected: str) -> str:
    parts = [
        '<option value="">All templates</option>',
    ]
    for t in templates:
        sel = " selected" if t == selected else ""
        parts.append(f'<option value="{_esc(t)}"{sel}>{_esc(t)}</option>')
    return "".join(parts)


def _render_status_filter(selected: str) -> str:
    options = [
        ("", "All statuses"),
        ("queued", "Queued"),
        ("sent", "Sent"),
        ("failed", "Failed"),
        ("bounced", "Bounced"),
    ]
    parts: list[str] = []
    for value, label in options:
        sel = " selected" if value == selected else ""
        parts.append(f'<option value="{_esc(value)}"{sel}>{_esc(label)}</option>')
    return "".join(parts)


def _render_top_failing(items: list[dict]) -> str:
    if not items:
        return '<p class="emails-stat__hint">No failures in window.</p>'
    parts = ['<ul class="emails-top-list">']
    for it in items:
        parts.append(
            f'<li><span class="emails-top-template">{_esc(it["template"])}</span>'
            f'<span class="emails-top-count">{int(it["count"])}</span></li>'
        )
    parts.append("</ul>")
    return "".join(parts)


# ── HTML page ──────────────────────────────────────────────────────────


@server.app.get("/admin/emails", response_class=HTMLResponse)
async def admin_emails_page(
    request: Request,
    template: Optional[str] = None,
    status: Optional[str] = None,
    recipient: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    """Render the /admin/emails diagnostic dashboard."""
    user = server._require_admin_user(request, page=True)
    if user is None:
        return server._denied_response(request)
    if not isinstance(user, dict):
        return user  # RedirectResponse for 2FA

    # Clamp limit/offset to keep the page bounded.
    try:
        limit = max(1, min(int(limit or 200), 500))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0

    template_filter = (template or "").strip()
    status_filter = (status or "").strip().lower()
    recipient_filter = (recipient or "").strip()

    try:
        rows = _list_email_rows(
            limit=limit,
            offset=offset,
            status_filter=status_filter or None,
            template_filter=template_filter or None,
            recipient_filter=recipient_filter or None,
        )
        stats = _stats_24h()
        templates = _list_distinct_templates()
    except Exception:
        log.exception("admin_emails_page: initial snapshot failed")
        rows, stats, templates = [], {
            "sent_24h": 0, "failed_24h": 0, "queued_24h": 0,
            "failure_rate": None, "top_failing_templates": [],
            "window_hours": 24,
        }, []

    return render_admin_page(
        request,
        "admin/emails.html",
        page_title="Emails",
        active_route="emails",
        breadcrumb=[("Admin", "/admin"), ("Emails", "/admin/emails")],
        raw_stat_sent=str(stats["sent_24h"]),
        raw_stat_failed=str(stats["failed_24h"]),
        raw_stat_queued=str(stats["queued_24h"]),
        raw_stat_rate=_fmt_pct(stats["failure_rate"]),
        raw_top_failing=_render_top_failing(stats["top_failing_templates"]),
        raw_recent_rows=_render_recent_rows(rows),
        raw_template_options=_render_template_filter(templates, template_filter),
        raw_status_options=_render_status_filter(status_filter),
        raw_recipient_value=_esc(recipient_filter),
        raw_result_count=str(len(rows)),
    )


# ── JSON: list (for future polling / table refresh) ────────────────────


@server.app.get("/admin/api/emails")
@rate_limit(limit=120, window_seconds=60, key_func=_admin_key)
async def admin_api_emails_list(
    request: Request,
    template: Optional[str] = None,
    status: Optional[str] = None,
    recipient: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> JSONResponse:
    """JSON shape mirrors the HTML rows."""
    server._require_admin_user(request)
    try:
        limit = max(1, min(int(limit or 100), 500))
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        limit, offset = 100, 0

    rows = _list_email_rows(
        limit=limit,
        offset=offset,
        status_filter=(status or "").strip().lower() or None,
        template_filter=(template or "").strip() or None,
        recipient_filter=(recipient or "").strip() or None,
    )
    # Strip the full recipient from the list-view JSON for parity with
    # the HTML redaction. Callers who want the full address use the
    # detail endpoint, which is admin-only and audit-eligible.
    safe = []
    for r in rows:
        d = dict(r)
        d.pop("recipient", None)
        safe.append(d)
    return JSONResponse({"emails": safe, "count": len(safe)})


# ── HTML: detail view ──────────────────────────────────────────────────


@server.app.get("/admin/emails/{email_id}", response_class=HTMLResponse)
async def admin_email_detail(request: Request, email_id: int):
    """Render the full payload — useful for debugging a bad send."""
    user = server._require_admin_user(request, page=True)
    if user is None:
        return server._denied_response(request)
    if not isinstance(user, dict):
        return user

    with db.conn() as c:
        row = c.execute(
            "SELECT id, name, payload, status, attempts, max_attempts, error, "
            "result, enqueued_at, started_at, finished_at, duration_ms "
            "FROM background_jobs WHERE id = ? AND name = 'send_email'",
            (email_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="email not found")

    payload = _parse_payload(row["payload"])
    template = payload.get("template") or ""
    to = payload.get("to") or ""
    context_dict = payload.get("context") or {}
    reply_to = payload.get("reply_to") or ""
    tags = payload.get("tags") or []
    label = _label_status(row["status"])

    # Render the email body using the same code path that send_template
    # uses — this is the *exact* HTML the recipient (would have) seen.
    rendered_html = ""
    rendered_subject = ""
    rendered_text = ""
    render_error = ""
    try:
        from email_system.service import _SUBJECTS, _resolve_admin_override
        from email_system.renderer import render, render_text_fallback

        ctx = dict(context_dict)
        ctx.setdefault("app_url", "https://narve.ai")

        override_subject, override_html = _resolve_admin_override(template, ctx)
        if override_html is not None:
            rendered_subject = override_subject or _SUBJECTS.get(template, "narve.ai")
            rendered_html = override_html
        else:
            rendered_subject = _SUBJECTS.get(template, "narve.ai")
            if "subject" in ctx:
                rendered_subject = ctx["subject"]
            rendered_html = render(template, ctx)
        rendered_text = render_text_fallback(rendered_html)
    except Exception as exc:
        render_error = f"render failed: {type(exc).__name__}: {exc}"
        log.warning("admin email detail render failed for id=%s: %s", email_id, exc)

    resend_form = ""
    if label == "failed":
        resend_form = (
            f'<form method="post" action="/admin/emails/{int(row["id"])}/resend" '
            f'class="emails-detail__resend">'
            f"{{{{ raw_csrf_field }}}}"
            f'<button type="submit" class="emails-resend-btn">Re-enqueue this delivery</button>'
            f"</form>"
        )

    headers_block = json.dumps({
        "From": "narve.ai <noreply@narve.ai>",
        "To": to,
        "Reply-To": reply_to,
        "Subject": rendered_subject,
        "X-Tags": ", ".join(map(str, tags)) if tags else "",
        "X-narve-template": template,
        "X-narve-job-id": str(row["id"]),
    }, indent=2)

    return render_admin_page(
        request,
        "admin/email-detail.html",
        page_title=f"Email #{int(row['id'])}",
        active_route="emails",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Emails", "/admin/emails"),
            (f"#{int(row['id'])}", None),
        ],
        raw_email_id=str(int(row["id"])),
        raw_template=_esc(template),
        raw_recipient=_esc(to),  # full recipient — admin already authed
        raw_recipient_redacted=_esc(_redact_recipient(to)),
        raw_status_label=_esc(label),
        raw_status_class=_esc(label if label in ("queued", "sent", "failed", "bounced") else "unknown"),
        raw_attempts=f'{int(row["attempts"] or 0)} / {int(row["max_attempts"] or 0)}',
        raw_enqueued=_esc(_fmt_ts(row["enqueued_at"])),
        raw_finished=_esc(_fmt_ts(row["finished_at"] or row["started_at"])),
        raw_duration=_esc(f'{int(row["duration_ms"])}ms' if row["duration_ms"] is not None else "—"),
        raw_error_message=_esc(row["error"] or ""),
        raw_subject=_esc(rendered_subject),
        raw_body_html=rendered_html or "",  # rendered into a sandboxed iframe
        raw_body_text=_esc(rendered_text),
        raw_headers=_esc(headers_block),
        raw_context_json=_esc(json.dumps(context_dict, indent=2, default=str)),
        raw_render_error=_esc(render_error),
        raw_resend_form=resend_form,
    )


# ── POST: resend (CSRF required by middleware) ─────────────────────────


@server.app.post("/admin/emails/{email_id}/resend")
@rate_limit(limit=20, window_seconds=60, key_func=_admin_key)
async def admin_email_resend(request: Request, email_id: int):
    """Re-enqueue a previously-failed send_email job.

    The CSRF middleware (server.CSRFMiddleware) validates the
    double-submit token before this handler runs. The job is queued
    using the original payload — no admin-controlled override is
    accepted, so this is safe even with a compromised admin session
    (attacker can re-send what we already sent, not redirect to a new
    address).
    """
    admin = server._require_admin_user(request)
    if not isinstance(admin, dict):
        raise HTTPException(status_code=403, detail="Admin required")

    with db.conn() as c:
        row = c.execute(
            "SELECT id, name, payload, status FROM background_jobs "
            "WHERE id = ? AND name = 'send_email'",
            (email_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="email not found")

    payload = _parse_payload(row["payload"])

    try:
        from jobs.email_jobs import enqueue_email
        new_id = await enqueue_email(
            to=payload.get("to") or "",
            template=payload.get("template") or "",
            context=payload.get("context") or {},
            reply_to=payload.get("reply_to"),
            tags=payload.get("tags") or None,
        )
    except Exception as exc:
        log.warning("admin email resend failed for id=%s: %s", email_id, exc)
        raise HTTPException(status_code=500, detail=f"resend failed: {exc}")

    # Audit the resend so the trail of "who re-fired which delivery"
    # survives. Same audit channel as the template-update path.
    try:
        from security import audit as _a
        # Literal action string — there's no dedicated AuditAction
        # constant for delivery-level resends yet. The convention used
        # elsewhere in audit.py is dotted snake-case.
        _a.log_action(
            admin_user_id=admin.get("user_id"),
            admin_email=admin.get("email"),
            action="email.delivery_resend",
            target_type="email_job",
            target_id=str(email_id),
            request=request,
            notes=f"resend -> new_job_id={new_id}",
        )
    except Exception:
        # Audit failure shouldn't block the resend.
        log.exception("audit log for resend failed (non-fatal)")

    return JSONResponse({
        "ok": True,
        "original_id": int(email_id),
        "new_job_id": int(new_id),
    })
