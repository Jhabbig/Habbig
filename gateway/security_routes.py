"""Security-surface routes — capture-attempt logging, admin forensics, user privacy prefs.

Registered via ``security_routes.register(app)`` from server.py. Keeps the
watermark + forensic machinery out of the 6700-line server.py while still
using its helpers for auth, CSRF, and rate limits.

Endpoints:

  POST /api/security/capture-attempt
      Accepts a small JSON envelope describing a client-side capture event
      (PrintScreen, Cmd+Shift+4, getDisplayMedia, devtools open, large
      clipboard copy). Writes to ``security_events``. Fires an admin email
      if the user crosses 5 events / 10 min. Authenticated-only.

  GET/POST /settings/privacy
      Per-user privacy preferences — blur-on-inactive, devtools blur.
      Watermarks + bulk-rate-limits are NOT toggleable (per spec).

  GET /admin/security/bulk-fetches
      Top 20 users by 24h row count, with a flagged list above.

  GET /admin/security/forensics
      Upload-a-screenshot form + results rendering.

  POST /admin/security/forensics/analyze
      Multipart upload handler for the forensics tool. Super-admin only,
      audit-logged.

The settings page stores preferences in ``users`` via two new columns —
``watermark_blur_enabled`` (default 1) and ``devtools_blur_enabled``
(default 1) — added with a one-shot idempotent ALTER at import time so we
don't need another migration file just for two booleans.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Optional

from fastapi import Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db


log = logging.getLogger(__name__)
security_log = logging.getLogger("security.capture")


# ── One-shot schema patch: user privacy toggle columns ──────────────────

def _ensure_user_privacy_columns() -> None:
    for col in ("watermark_blur_enabled", "devtools_blur_enabled"):
        try:
            with db.conn() as c:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass  # Column already exists.


_ensure_user_privacy_columns()


# ── DB helpers (kept here so db.py stays out of the edit surface) ──────

def record_security_event(
    *,
    user_id: Optional[int],
    event_type: str,
    metadata: dict,
    ip: str,
    user_agent: str,
) -> int:
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO security_events "
            "(user_id, event_type, metadata, ip, user_agent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, event_type[:64], json.dumps(metadata)[:4096],
             (ip or "")[:64], (user_agent or "")[:256], now),
        )
        return cur.lastrowid


def recent_events_for_user(user_id: int, window_seconds: int = 600) -> int:
    now = int(time.time())
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM security_events "
            "WHERE user_id = ? AND created_at >= ?",
            (user_id, now - window_seconds),
        ).fetchone()
    return int(row["n"]) if row else 0


def upsert_watermark_seed(user_id: int, session_id: str, seed: int) -> None:
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO watermark_seeds "
            "(user_id, session_id, seed, generated_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, session_id) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at",
            (user_id, session_id, seed, now, now),
        )


def get_user_privacy_prefs(user_id: int) -> dict:
    with db.conn() as c:
        row = c.execute(
            "SELECT watermark_blur_enabled, devtools_blur_enabled "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"inactive_blur": True, "devtools_blur": True}
    return {
        "inactive_blur": bool(row["watermark_blur_enabled"]),
        "devtools_blur": bool(row["devtools_blur_enabled"]),
    }


def set_user_privacy_prefs(user_id: int, *, inactive_blur: bool, devtools_blur: bool) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE users SET watermark_blur_enabled = ?, devtools_blur_enabled = ? "
            "WHERE id = ?",
            (1 if inactive_blur else 0, 1 if devtools_blur else 0, user_id),
        )


def top_bulk_fetchers(limit: int = 20) -> list[dict]:
    """Top N users by row count in the last 24h."""
    cutoff = int(time.time()) - 86400
    with db.conn() as c:
        rows = c.execute(
            "SELECT user_id, SUM(rows_fetched) AS total, MAX(flagged) AS flagged "
            "FROM bulk_fetch_counters "
            "WHERE window_start >= ? "
            "GROUP BY user_id ORDER BY total DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    out = []
    for r in rows:
        user = db.get_user_by_id(r["user_id"])
        out.append({
            "user_id": r["user_id"],
            "email": (user["email"] if user else "unknown"),
            "rows_24h": int(r["total"]),
            "flagged": bool(r["flagged"]),
        })
    return out


# ── Route handlers ──────────────────────────────────────────────────────

async def capture_attempt(request: Request) -> JSONResponse:
    """POST /api/security/capture-attempt — log a client-side capture event.

    Authenticated-only (we don't care about anonymous attempts — they have
    no data to capture). Intentionally lightweight: the client fires these
    in response to keydown/visibilitychange events and we don't want to
    block the UI thread on slow DB writes.
    """
    from server import current_user, _get_client_ip, _is_rate_limited  # type: ignore

    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    user_id = user["user_id"]
    ip = _get_client_ip(request)

    # Per-user rate cap — don't let a misbehaving client hammer this.
    if _is_rate_limited(f"capture-attempt:{user_id}", limit=60, window=60):
        return JSONResponse({"throttled": True}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        body = {}
    event_type = (body.get("type") or "").strip()[:64] or "unknown"
    metadata = {k: v for k, v in (body or {}).items() if k != "type"}

    try:
        record_security_event(
            user_id=user_id,
            event_type=event_type,
            metadata=metadata,
            ip=ip,
            user_agent=(request.headers.get("user-agent") or "")[:256],
        )
    except Exception as exc:
        log.warning("security event write failed uid=%s: %s", user_id, exc)
        return JSONResponse({"ok": False})

    security_log.warning(
        "capture_attempt user_id=%s type=%s ip=%s metadata_keys=%s",
        user_id, event_type, ip, list(metadata.keys()),
    )

    # Realtime fan-out to admin:security. Best-effort — a hub failure
    # never blocks the capture-attempt response.
    try:
        from realtime.broadcast import emit_capture_attempt
        emit_capture_attempt(
            user_id=user_id,
            kind=event_type,
            context={"ip": ip, "metadata_keys": list(metadata.keys())},
        )
    except Exception:
        pass

    # Flood alert: >5 events / 10 min → fire an admin notification.
    try:
        count = recent_events_for_user(user_id, window_seconds=600)
        if count == 6:  # fire once, on the threshold crossing
            import asyncio as _asyncio
            _asyncio.create_task(
                _notify_admin_of_flood(user_id, user.get("email") or "", count)
            )
    except Exception:
        pass

    return JSONResponse({"ok": True})


async def _notify_admin_of_flood(user_id: int, email: str, count: int) -> None:
    """Fire-and-forget: log at ERROR level + enqueue an admin email.

    ``enqueue_email`` is async in jobs/email_jobs.py, so this must be
    awaited from an async context. Callers wrap the await in a task so a
    stalled email backend never blocks the security-event write.
    """
    log.error(
        "capture_attempt FLOOD user_id=%s email=%s events_in_10min=%s",
        user_id, email, count,
    )
    try:
        from jobs.email_jobs import enqueue_email
        await enqueue_email(
            to="security@narve.ai",
            template="admin_security_alert",
            context={"user_id": user_id, "user_email": email, "count": count},
            tags=["security", "flood"],
        )
    except Exception as exc:
        log.warning("admin flood notify failed user_id=%s: %s", user_id, exc)


async def settings_privacy_toggles_post(
    request: Request,
    inactive_blur: str = Form(""),
    devtools_blur: str = Form(""),
) -> RedirectResponse:
    """POST /settings/privacy/toggles — save watermark/blur preferences.

    Lives at a sub-path because ``/settings/privacy`` itself is owned by
    export_routes.py (data-export UI). We cohabit the same template page;
    this handler only persists the toggle state and redirects back.
    """
    from server import current_user  # type: ignore
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    set_user_privacy_prefs(
        user["user_id"],
        inactive_blur=(inactive_blur == "on" or inactive_blur == "1"),
        devtools_blur=(devtools_blur == "on" or devtools_blur == "1"),
    )
    return RedirectResponse("/settings/privacy?saved=1", status_code=302)


async def admin_bulk_fetches(request: Request) -> HTMLResponse:
    from server import _require_admin_user, render_page  # type: ignore
    admin = _require_admin_user(request, page=True)
    top = top_bulk_fetchers(limit=20)
    rows_html = []
    for row in top:
        badge = '<span class="nv-pill nv-pill-warn">FLAGGED</span>' if row["flagged"] else ''
        rows_html.append(
            f'<tr><td>{row["user_id"]}</td>'
            f'<td>{_escape(row["email"])}</td>'
            f'<td style="text-align:right">{row["rows_24h"]:,}</td>'
            f'<td>{badge}</td></tr>'
        )
    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/security-bulk.html",
        page_title="Bulk fetches",
        active_route="bulk",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Security", None),
            ("Bulk fetches", "/admin/security/bulk-fetches"),
        ],
        raw_rows="\n".join(rows_html) or '<tr><td colspan="4" style="opacity:0.6">No bulk fetches in the last 24h.</td></tr>',
    )


async def admin_forensics_get(request: Request) -> HTMLResponse:
    from server import _require_super_admin  # type: ignore
    from admin_shell import render_admin_page
    admin = _require_super_admin(request)
    return render_admin_page(
        request,
        "admin/security-forensics.html",
        page_title="Forensics",
        active_route="forensics",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Security", None),
            ("Forensics", "/admin/security/forensics"),
        ],
        raw_result="",
    )


async def admin_forensics_analyze(
    request: Request,
    screenshot: UploadFile = File(None),
    text_blob: str = Form(""),
    payload_json: str = Form(""),
) -> HTMLResponse:
    from server import _require_super_admin, render_page  # type: ignore
    from security import audit as _audit
    admin = _require_super_admin(request)

    image_bytes = None
    if screenshot is not None:
        try:
            image_bytes = await screenshot.read()
        except Exception:
            image_bytes = None

    payload = None
    if payload_json.strip():
        try:
            parsed = json.loads(payload_json)
            if isinstance(parsed, list):
                payload = parsed
        except Exception:
            payload = None

    from forensics import extract_watermark
    result = extract_watermark.identify_leak(
        image_bytes=image_bytes,
        text=text_blob or None,
        payload=payload,
    )

    # Audit every use of the forensics tool — super-admin-only, but still a
    # sensitive operation (reveals who saw what).
    try:
        _audit.log_action(
            admin_user_id=admin["user_id"], admin_email=admin["email"],
            action="forensics.analyze",
            target_type="user", target_id=result.get("user_id") or 0,
            target_description=(result.get("email") or "unknown"),
            before=None, after={"confidence": result.get("confidence")},
            request=request,
        )
    except Exception:
        pass

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/security-forensics.html",
        page_title="Forensics",
        active_route="forensics",
        breadcrumb=[
            ("Admin", "/admin"),
            ("Security", None),
            ("Forensics", "/admin/security/forensics"),
        ],
        raw_result=_render_forensics_result(result),
    )


def _render_forensics_result(result: dict) -> str:
    if not result.get("user_id"):
        return (
            '<div class="nv-forensics-result nv-forensics-nohit">'
            '<strong>No match.</strong> '
            + _escape(result.get("evidence", ["No evidence surfaced."])[0] if result.get("evidence") else "") +
            '</div>'
        )
    evidence = "".join(f"<li>{_escape(e)}</li>" for e in result.get("evidence", []))
    confidence = result.get("confidence", 0.0)
    badge_cls = "nv-pill-ok" if confidence >= 0.9 else ("nv-pill-warn" if confidence >= 0.7 else "nv-pill-faint")
    return (
        '<div class="nv-forensics-result">'
        f'<div class="nv-forensics-headline">Highest-likelihood source: '
        f'<strong>user_id={result["user_id"]}</strong> ({_escape(result.get("email") or "unknown")}) '
        f'<span class="nv-pill {badge_cls}">confidence {confidence:.2f}</span></div>'
        f'<div class="nv-forensics-source">Source: {_escape(result.get("source", "unknown"))}</div>'
        f'<ul class="nv-forensics-evidence">{evidence}</ul>'
        '</div>'
    )


def _escape(s: str) -> str:
    import html as _h
    return _h.escape(str(s or ""))


# ── Registration ────────────────────────────────────────────────────────

def register(app) -> None:
    app.post("/api/security/capture-attempt")(capture_attempt)
    app.post("/settings/privacy/toggles")(settings_privacy_toggles_post)
    app.get("/admin/security/bulk-fetches", response_class=HTMLResponse)(admin_bulk_fetches)
    app.get("/admin/security/forensics", response_class=HTMLResponse)(admin_forensics_get)
    app.post("/admin/security/forensics/analyze", response_class=HTMLResponse)(admin_forensics_analyze)
