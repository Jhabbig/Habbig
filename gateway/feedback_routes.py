"""Public feedback + roadmap + admin triage.

Registers via side-effect-of-import (same pattern as billing_routes and
engagement_routes). server.py does a single `import feedback_routes`
before the catch-all so these paths land on the apex rather than being
swallowed as 404s.

Surface area:

    GET  /feedback                         → list (filterable + sortable)
    GET  /feedback/{item_id}               → detail (comments + vote)
    POST /api/feedback                     → submit new item (authed)
    POST /api/feedback/{id}/vote           → toggle upvote (subscriber only)
    POST /api/feedback/{id}/comment        → user comment
    GET  /admin/feedback                   → triage page
    POST /admin/feedback/{id}/status       → set status (+ notify submitter)
    POST /admin/feedback/{id}/duplicate    → mark as dup of another item
    POST /admin/feedback/{id}/comment      → admin response (+ notify)
    POST /admin/feedback/{id}/ship         → mark shipped + link commit sha

Upvotes require an active paid plan — free accounts can still submit and
read, but shouldn't be able to mass-upvote. Enforcement happens here in
the route handlers, not in the DB layer (see ``_require_subscriber``).

Notifications fire on every status change + admin comment via the
existing notifications table. No create_notification DB helper exists
yet in this tree, so we insert directly — matches the schema expected
by notification_routes.py's SELECT.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import logging
import time
from typing import Any, Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import server
from server import (
    app,
    render_page,
    current_user,
    get_subdomain,
    proxy_request,
    _user_plan_info,
    _role_badge,
)


log = logging.getLogger("feedback")


# Canonical enums. Keep these in sync with the migration-130 comments.
VALID_TYPES = frozenset({"bug", "feature", "question"})
VALID_STATUSES = frozenset({"open", "in_progress", "shipped", "declined", "dup"})

STATUS_LABELS = {
    "open":        ("○",  "OPEN",        "var(--text-muted)"),
    "in_progress": ("⚙",  "IN PROGRESS", "#f59e0b"),
    "shipped":     ("✓",  "SHIPPED",     "#10b981"),
    "declined":    ("✕",  "DECLINED",    "#ef4444"),
    "dup":         ("↗",  "DUPLICATE",   "var(--text-muted)"),
}

TYPE_LABELS = {
    "bug":      "Bug",
    "feature":  "Feature idea",
    "question": "Question",
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _is_subscriber(user: dict) -> bool:
    """True if the user can upvote — any active paid plan OR admin."""
    if user.get("is_admin"):
        return True
    try:
        subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
        pinfo = _user_plan_info(user, subs, int(time.time()))
        return bool(pinfo.get("plan"))
    except Exception:
        return False


def _require_subscriber(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _is_subscriber(user):
        raise HTTPException(
            status_code=402,
            detail="Upvoting requires an active subscription. Submit freely.",
        )
    return user


def _notify_submitter(user_id: Optional[int], item_id: int, event: str, extra: Optional[dict] = None) -> None:
    """Write a notifications-table row for the feedback submitter.

    We insert directly rather than going through notifications.create_notification
    because that helper references NOTIFICATION_TYPES + a DB function that
    don't exist in this branch yet. Direct insert matches the schema
    selected by notification_routes.py, so the bell UI + read/archive
    handlers pick it up seamlessly.
    """
    if not user_id:
        return

    titles = {
        "status":       "Feedback status updated",
        "admin_reply":  "Team replied to your feedback",
        "shipped":      "Your feedback has shipped",
    }
    title = titles.get(event, "Feedback update")

    meta = {"feedback_id": item_id, "event": event}
    if extra:
        meta.update(extra)

    body = extra.get("status") if extra else ""
    if event == "shipped":
        body = "Your suggestion is live. Thanks for helping shape the product."
    elif event == "admin_reply":
        body = (extra or {}).get("excerpt") or "A team member replied to your feedback."
    elif event == "status" and extra:
        body = f"New status: {extra.get('status', '')}"

    try:
        with db.conn() as c:
            c.execute(
                """
                INSERT INTO notifications
                  (user_id, type, title, body, link_url, metadata, created_at, archived)
                VALUES (?, 'feedback', ?, ?, ?, ?, ?, 0)
                """,
                (
                    user_id,
                    title[:200],
                    (body or "")[:500],
                    f"/feedback/{item_id}",
                    json.dumps(meta),
                    int(time.time()),
                ),
            )
    except Exception as exc:
        # Notifications are best-effort — the admin action still succeeds.
        log.warning("feedback notify failed uid=%s item=%s: %s", user_id, item_id, exc)


def _user_has_voted(user_id: int, item_id: int) -> bool:
    with db.conn() as c:
        row = c.execute(
            "SELECT 1 FROM feedback_votes WHERE user_id = ? AND feedback_id = ?",
            (user_id, item_id),
        ).fetchone()
    return bool(row)


def _sanitize_type(raw: str) -> str:
    raw = (raw or "").strip().lower()
    return raw if raw in VALID_TYPES else "feature"


def _sanitize_status(raw: str) -> Optional[str]:
    raw = (raw or "").strip().lower()
    return raw if raw in VALID_STATUSES else None


def _list_items(
    *,
    status: Optional[str] = None,
    type_: Optional[str] = None,
    sort: str = "top",
    include_private: bool = False,
    user_id: Optional[int] = None,
    q: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """List feedback items with optional filters.

    ``user_id`` scopes to a single author — used by the /feedback?mine=1
    view (#3) and by admin-filtered views. Passing user_id implicitly
    drops the is_public=1 gate because the user can see their own
    private submissions.

    ``q`` is a case-insensitive LIKE search on title, used by the
    similar-items hint (#5). Max 60 chars so the pattern stays cheap.
    """
    where = []
    params: list[Any] = []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    elif not include_private:
        where.append("is_public = 1")
    if status and status in VALID_STATUSES:
        where.append("status = ?")
        params.append(status)
    if type_ and type_ in VALID_TYPES:
        where.append("type = ?")
        params.append(type_)
    if q:
        q_clean = q.strip()[:60]
        if q_clean:
            where.append("LOWER(title) LIKE ?")
            params.append(f"%{q_clean.lower()}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    order_sql = {
        "top":    "upvotes DESC, created_at DESC",
        "new":    "created_at DESC",
        "recent": "updated_at DESC",
    }.get(sort, "upvotes DESC, created_at DESC")

    with db.conn() as c:
        rows = c.execute(
            f"SELECT * FROM feedback_items {where_sql} ORDER BY {order_sql} LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _load_comments(item_id: int) -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM feedback_comments WHERE feedback_id = ? ORDER BY created_at ASC",
            (item_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── HTML render helpers ──────────────────────────────────────────────────────


def _render_status_chip(status: str) -> str:
    icon, label, color = STATUS_LABELS.get(status, ("○", status.upper(), "var(--text-muted)"))
    return (
        f'<span class="fb-status" style="display:inline-flex;align-items:center;gap:6px;'
        f'color:{color};font-size:11px;font-weight:700;letter-spacing:0.04em;'
        f'text-transform:uppercase">'
        f'<span aria-hidden="true">{icon}</span>{html.escape(label)}</span>'
    )


def _render_type_pill(type_: str) -> str:
    bg = {
        "bug":      "rgba(239,68,68,0.12)",
        "feature":  "rgba(59,130,246,0.12)",
        "question": "rgba(168,85,247,0.12)",
    }.get(type_, "var(--interactive-ghost)")
    return (
        f'<span style="display:inline-block;padding:2px 8px;background:{bg};'
        f'border-radius:10px;font-size:10px;font-weight:600;color:var(--text-primary);'
        f'text-transform:uppercase;letter-spacing:0.04em">'
        f'{html.escape(TYPE_LABELS.get(type_, type_))}</span>'
    )


def _render_list_row(item: dict, voted_ids: set[int]) -> str:
    voted = item["id"] in voted_ids
    vote_mark = ' · <span style="color:var(--accent);font-weight:600">you voted</span>' if voted else ""
    shipped_badge = ""
    if item["status"] == "shipped" and item.get("shipped_commit_sha"):
        sha = html.escape(item["shipped_commit_sha"][:7])
        shipped_badge = f' · <span style="color:var(--text-muted);font-family:ui-monospace,monospace;font-size:11px">{sha}</span>'
    return (
        f'<a class="fb-row" href="/feedback/{item["id"]}" '
        f'style="display:flex;align-items:center;gap:16px;padding:14px 18px;'
        f'border-bottom:1px solid var(--border);text-decoration:none;color:inherit;transition:background 0.12s">'
        f'<div style="flex:none;width:72px">{_render_status_chip(item["status"])}</div>'
        f'<div style="flex:1;min-width:0">'
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">'
        f'{_render_type_pill(item["type"])}'
        f'<span style="font-size:14px;font-weight:600;color:var(--text-primary)">{html.escape(item["title"][:140])}</span>'
        f'</div>'
        f'<div style="font-size:12px;color:var(--text-muted)">'
        f'{html.escape((item["body"] or "")[:160])}{vote_mark}{shipped_badge}'
        f'</div></div>'
        f'<div style="flex:none;min-width:60px;text-align:right">'
        f'<div style="font-size:18px;font-weight:700;font-variant-numeric:tabular-nums">{item["upvotes"]}</div>'
        f'<div style="font-size:10px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.04em">upvotes</div>'
        f'</div></a>'
    )


# ── Public routes ────────────────────────────────────────────────────────────


@app.get("/feedback", response_class=HTMLResponse, include_in_schema=False)
async def feedback_list_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/feedback")
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)

    status = request.query_params.get("status")
    type_ = request.query_params.get("type")
    sort = request.query_params.get("sort", "top")
    # ENHANCEMENT #3 — ?mine=1 scopes the list to the current user's
    # own submissions (public + private). The user_id filter in
    # _list_items bypasses the is_public gate, so private posts show up
    # for their author.
    mine = request.query_params.get("mine") == "1"

    items = _list_items(
        status=_sanitize_status(status) if status else None,
        type_=_sanitize_type(type_) if type_ and type_ != "all" else None,
        sort=sort,
        include_private=False,
        user_id=(user["user_id"] if mine else None),
        limit=200,
    )

    voted_ids: set[int] = set()
    if items:
        with db.conn() as c:
            rows = c.execute(
                "SELECT feedback_id FROM feedback_votes WHERE user_id = ? "
                "AND feedback_id IN (" + ",".join("?" * len(items)) + ")",
                (user["user_id"], *[i["id"] for i in items]),
            ).fetchall()
        voted_ids = {int(r["feedback_id"]) for r in rows}

    rows_html = "".join(_render_list_row(i, voted_ids) for i in items) or (
        '<div style="padding:48px 24px;text-align:center;color:var(--text-muted);font-size:13px">'
        'No feedback yet. Be the first to share.</div>'
    )

    def chip(label: str, param: str, value: str, active: bool) -> str:
        qs = f"?{param}={value}" if value else ""
        cls = "fb-chip fb-chip-active" if active else "fb-chip"
        return f'<a class="{cls}" href="/feedback{qs}">{html.escape(label)}</a>'

    type_nav = [
        chip("All",        "type", "",         not type_ or type_ == "all"),
        chip("Bugs",       "type", "bug",      type_ == "bug"),
        chip("Features",   "type", "feature",  type_ == "feature"),
        chip("Questions",  "type", "question", type_ == "question"),
    ]
    # "Mine" is a separate axis from type/status — reuses the chip
    # factory but with its own parameter. The href flips ?mine= on/off
    # so clicking it again toggles back to the full list.
    mine_href = "/feedback" if mine else "/feedback?mine=1"
    mine_cls = "fb-chip fb-chip-active" if mine else "fb-chip"
    mine_chip = f'<a class="{mine_cls}" href="{mine_href}">My submissions</a>'
    status_nav = [
        chip("Open",         "status", "open",         status == "open"),
        chip("In progress",  "status", "in_progress",  status == "in_progress"),
        chip("Shipped",      "status", "shipped",      status == "shipped"),
        chip("Declined",     "status", "declined",     status == "declined"),
    ]
    sort_nav = [
        chip("Top voted",       "sort", "top",    sort == "top"),
        chip("Newest",          "sort", "new",    sort == "new"),
        chip("Recently updated", "sort", "recent", sort == "recent"),
    ]

    # Admin sees a pass-through link to /admin/feedback so they don't
    # have to backtrack through the main admin nav.
    admin_link = ""
    if user.get("is_admin"):
        admin_link = ' · <a href="/admin/feedback" style="color:var(--text-muted);font-size:12px">Admin</a>'

    return render_page(
        "feedback",
        request=request,
        username=user.get("username", user["email"]),
        raw_nav_role=_role_badge(user),
        _is_admin=user.get("is_admin"),
        raw_admin_link=('<a href="/admin">Admin</a>' if user.get("is_admin") else ""),
        raw_rows=rows_html,
        raw_type_nav="".join(type_nav) + mine_chip,
        raw_status_nav="".join(status_nav),
        raw_sort_nav="".join(sort_nav),
        raw_admin_passthrough=admin_link,
    )


@app.get("/feedback/{item_id}", response_class=HTMLResponse, include_in_schema=False)
async def feedback_detail_page(request: Request, item_id: int):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, f"/feedback/{item_id}")
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)

    with db.conn() as c:
        row = c.execute("SELECT * FROM feedback_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Feedback item not found")
    item = dict(row)

    # Private items only visible to the submitter or an admin.
    if not item["is_public"]:
        is_owner = item["user_id"] == user["user_id"]
        if not (is_owner or user.get("is_admin")):
            raise HTTPException(status_code=404, detail="Feedback item not found")

    comments = _load_comments(item_id)
    voted = _user_has_voted(user["user_id"], item_id)
    can_vote = _is_subscriber(user) and not voted

    comment_html = []
    for com in comments:
        who = "Team" if com["is_admin"] else "User"
        bg = "rgba(59,130,246,0.06)" if com["is_admin"] else "transparent"
        comment_html.append(
            f'<div style="padding:14px 16px;background:{bg};border:1px solid var(--border);'
            f'border-radius:8px;margin-bottom:8px">'
            f'<div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;'
            f'letter-spacing:0.05em;margin-bottom:6px">{who} · {html.escape(str(com["created_at"] or ""))}</div>'
            f'<div style="font-size:13px;line-height:1.55;white-space:pre-wrap">'
            f'{html.escape(com["body"])}</div></div>'
        )
    comments_html = "".join(comment_html) or (
        '<div style="padding:16px;text-align:center;color:var(--text-muted);font-size:12px">'
        'No comments yet.</div>'
    )

    vote_form = ""
    if voted:
        vote_form = (
            '<form method="post" action="/api/feedback/' + str(item_id) + '/vote" '
            'style="display:inline"><input type="hidden" name="toggle" value="1">'
            '<button class="sb-btn sb-btn-outline" type="submit">✓ Voted · Remove</button></form>'
        )
    elif can_vote:
        vote_form = (
            '<form method="post" action="/api/feedback/' + str(item_id) + '/vote" '
            'style="display:inline"><input type="hidden" name="toggle" value="1">'
            '<button class="sb-btn sb-btn-primary" type="submit">▲ Upvote</button></form>'
        )
    else:
        vote_form = (
            '<a href="/billing" class="sb-btn sb-btn-outline" title="Upvoting requires a subscription">'
            'Subscribe to vote</a>'
        )

    detail_body = (
        f'<div style="display:flex;gap:10px;align-items:center;margin-bottom:8px">'
        f'{_render_type_pill(item["type"])}{_render_status_chip(item["status"])}'
        f'</div>'
        f'<h1 style="margin:0 0 12px;font-family:var(--font-display);font-size:24px;'
        f'letter-spacing:-0.01em">{html.escape(item["title"])}</h1>'
        f'<div style="font-size:13px;line-height:1.6;color:var(--text-primary);'
        f'white-space:pre-wrap;margin-bottom:20px">{html.escape(item["body"])}</div>'
        f'<div style="display:flex;gap:12px;align-items:center;margin-bottom:24px">'
        f'<span style="font-size:20px;font-weight:700;font-variant-numeric:tabular-nums">'
        f'{item["upvotes"]} upvotes</span>{vote_form}</div>'
    )

    if item.get("admin_note"):
        detail_body += (
            '<div style="padding:14px 16px;background:rgba(16,185,129,0.08);'
            'border:1px solid rgba(16,185,129,0.3);border-radius:8px;margin-bottom:20px">'
            '<div style="font-size:11px;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.05em;color:#10b981;margin-bottom:6px">Team response</div>'
            f'<div style="font-size:13px;line-height:1.55;white-space:pre-wrap">'
            f'{html.escape(item["admin_note"])}</div></div>'
        )

    return render_page(
        "feedback-detail",
        request=request,
        username=user.get("username", user["email"]),
        raw_nav_role=_role_badge(user),
        _is_admin=user.get("is_admin"),
        raw_admin_link=('<a href="/admin">Admin</a>' if user.get("is_admin") else ""),
        raw_detail=detail_body,
        raw_comments=comments_html,
        feedback_id=str(item_id),
    )


@app.get("/api/feedback/search", include_in_schema=False)
async def api_feedback_search(request: Request, q: str = ""):
    """ENHANCEMENT #5 — similar-items hint for the submit modal.

    The feedback button's modal calls this on title-input blur and
    renders up to 3 existing items with matching titles to nudge the
    user away from duplicate submissions. Only searches public items —
    a private dup can't conflict with a public one.

    Rate-limited implicitly by the modal (debounced, single call on
    blur). Returns {items: [{id, title, status, upvotes}]}.
    """
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    q_clean = (q or "").strip()[:60]
    if len(q_clean) < 3:
        return JSONResponse({"items": []})
    items = _list_items(q=q_clean, sort="top", include_private=False, limit=3)
    return JSONResponse({
        "items": [
            {
                "id": i["id"],
                "title": i["title"],
                "status": i["status"],
                "upvotes": i["upvotes"],
            }
            for i in items
        ],
    })


@app.post("/api/feedback", include_in_schema=False)
async def api_feedback_submit(
    request: Request,
    type: str = Form(...),
    title: str = Form(...),
    body: str = Form(...),
    is_public: str = Form("1"),
):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # ENHANCEMENT #1 — Submission rate limit. A compromised session or
    # a bored user can otherwise fill the admin triage inbox. 10/hour is
    # generous for legitimate use (a user reporting a burst of bugs)
    # and still caps the blast radius. Keyed on user_id rather than IP
    # so a VPN-hopping attacker can't evade. Tests can bypass via the
    # FEEDBACK_RATELIMIT_DISABLED env flag.
    import os as _os
    if _os.environ.get("FEEDBACK_RATELIMIT_DISABLED") != "1":
        try:
            if server._is_rate_limited(
                f"feedback-submit:{user['user_id']}",
                limit=10, window=3600,
            ):
                raise HTTPException(
                    status_code=429,
                    detail="Too many submissions. Try again in an hour.",
                )
        except AttributeError:
            pass  # Older server.py without the helper — fall through.

    type_clean = _sanitize_type(type)

    # Route title + body through the shared normaliser. This catches:
    #   * NFC-normalises precomposed vs combining unicode (so "café"
    #     looks the same regardless of source keyboard).
    #   * Strips zero-width / BOM / bidi-control glyphs that would
    #     otherwise let an attacker sneak extra codepoints past the
    #     length cap.
    #   * Rejects null bytes / C0 control chars with a clean 400
    #     instead of a 500 from a downstream library.
    # `required=True` on both fields keeps the "must have title+body"
    # invariant; no longer need a separate empty-string check below.
    from security.input_hygiene import clean_text
    try:
        title_clean = clean_text(
            title, max_len=200, required=True, field="title",
        )
        body_clean = clean_text(
            body, max_len=4000, required=True, field="body",
        )
    except HTTPException:
        raise
    public_flag = 1 if (is_public or "0").lower() in ("1", "true", "yes", "on") else 0

    with db.conn() as c:
        cur = c.execute(
            """
            INSERT INTO feedback_items
              (user_id, type, title, body, is_public)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user["user_id"], type_clean, title_clean, body_clean, public_flag),
        )
        new_id = int(cur.lastrowid or 0)

    log.info(
        "feedback submitted uid=%s type=%s public=%s id=%s",
        user["user_id"], type_clean, public_flag, new_id,
    )
    # ENHANCEMENT #4 — record an engagement event so the churn-signal
    # job sees feedback submission as active behaviour. Fire-and-forget
    # so any failure in engagement.py can't break the submit flow.
    try:
        from engagement import log_event
        log_event(
            user["user_id"], "feedback_submit",
            metadata={"id": new_id, "type": type_clean, "public": bool(public_flag)},
        )
    except Exception:
        pass
    # XHR submissions get JSON; classic form POSTs get a redirect.
    wants_json = "application/json" in (request.headers.get("accept") or "")
    if wants_json:
        return JSONResponse({"ok": True, "id": new_id, "is_public": bool(public_flag)})
    target = f"/feedback/{new_id}" if public_flag else "/feedback?saved=private"
    return RedirectResponse(target, status_code=302)


@app.post("/api/feedback/{item_id}/vote", include_in_schema=False)
async def api_feedback_vote(request: Request, item_id: int, toggle: str = Form("1")):
    """Toggle upvote. Subscriber-only. Idempotent: calling twice un-votes.

    ENHANCEMENT #2 — rejects self-votes. An author upvoting their own
    submission biases the top-voted sort by author volume rather than
    community interest, which defeats the whole point of the page.
    """
    user = _require_subscriber(request)
    with db.conn() as c:
        row = c.execute(
            "SELECT id, user_id FROM feedback_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Feedback item not found")
        if row["user_id"] == user["user_id"]:
            raise HTTPException(
                status_code=400,
                detail="You can't upvote your own feedback.",
            )
        existing = c.execute(
            "SELECT 1 FROM feedback_votes WHERE user_id = ? AND feedback_id = ?",
            (user["user_id"], item_id),
        ).fetchone()
        if existing:
            c.execute(
                "DELETE FROM feedback_votes WHERE user_id = ? AND feedback_id = ?",
                (user["user_id"], item_id),
            )
            c.execute(
                "UPDATE feedback_items SET upvotes = MAX(0, upvotes - 1), updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (item_id,),
            )
            voted = False
        else:
            c.execute(
                "INSERT INTO feedback_votes (user_id, feedback_id) VALUES (?, ?)",
                (user["user_id"], item_id),
            )
            c.execute(
                "UPDATE feedback_items SET upvotes = upvotes + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (item_id,),
            )
            voted = True
        new_count_row = c.execute(
            "SELECT upvotes FROM feedback_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        new_count = int(new_count_row["upvotes"] if new_count_row else 0)

    # ENHANCEMENT #4 — engagement signal. Only log the "added a vote"
    # direction; un-voting isn't meaningful churn data and would just
    # noise up the histogram.
    if voted:
        try:
            from engagement import log_event
            log_event(user["user_id"], "feedback_vote", metadata={"id": item_id})
        except Exception:
            pass

    wants_json = "application/json" in (request.headers.get("accept") or "")
    if wants_json:
        return JSONResponse({"voted": voted, "upvotes": new_count})
    return RedirectResponse(f"/feedback/{item_id}", status_code=302)


@app.post("/api/feedback/{item_id}/comment", include_in_schema=False)
async def api_feedback_comment(request: Request, item_id: int, body: str = Form(...)):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    body_clean = (body or "").strip()[:2000]
    if not body_clean:
        raise HTTPException(status_code=400, detail="Comment cannot be empty")
    with db.conn() as c:
        row = c.execute("SELECT id, user_id FROM feedback_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Feedback item not found")
        c.execute(
            "INSERT INTO feedback_comments (feedback_id, user_id, body, is_admin) "
            "VALUES (?, ?, ?, 0)",
            (item_id, user["user_id"], body_clean),
        )
        c.execute(
            "UPDATE feedback_items SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (item_id,),
        )
    return RedirectResponse(f"/feedback/{item_id}", status_code=302)


# ── Admin routes ─────────────────────────────────────────────────────────────


def _require_admin(request: Request) -> dict:
    """Admin gate that doesn't trigger the 2FA redirect path — we return
    403 on the JSON admin POSTs so the UI can surface a clear error."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@app.get("/admin/feedback", response_class=HTMLResponse, include_in_schema=False)
async def admin_feedback_page(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return RedirectResponse("/token", status_code=302)

    filter_status = request.query_params.get("status")
    items = _list_items(
        status=_sanitize_status(filter_status) if filter_status else None,
        sort="new",
        include_private=True,
        limit=300,
    )

    # ENHANCEMENT #6 — rows are wrapped in a single <form> below so a
    # checkbox column enables bulk status change. Each row now starts
    # with a hidden checkbox bound to the same form as the footer
    # "Apply" button.
    rows_html: list[str] = []
    for item in items:
        pub_badge = ("public" if item["is_public"] else "private")
        pub_color = ("var(--text-muted)" if item["is_public"] else "#ef4444")
        reason = f" · dup of #{item['duplicate_of']}" if item.get("duplicate_of") else ""
        note_preview = html.escape((item.get("admin_note") or "")[:120])
        rows_html.append(
            f'<div class="af-row" data-id="{item["id"]}" style="padding:14px 16px;'
            f'border-bottom:1px solid var(--border);display:grid;'
            f'grid-template-columns:28px 80px 1fr 160px;gap:14px;align-items:center">'
            f'<input type="checkbox" name="ids" value="{item["id"]}" form="af-bulk" '
            f'aria-label="Select feedback #{item["id"]}" style="accent-color:var(--text-primary)">'
            f'<div>{_render_status_chip(item["status"])}</div>'
            f'<div style="min-width:0">'
            f'<div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">'
            f'{_render_type_pill(item["type"])}'
            f'<a href="/feedback/{item["id"]}" style="font-size:13px;font-weight:600;'
            f'color:var(--text-primary);text-decoration:none">{html.escape(item["title"][:120])}</a>'
            f'<span style="font-size:10px;color:{pub_color};text-transform:uppercase">'
            f'· {pub_badge}</span></div>'
            f'<div style="font-size:12px;color:var(--text-muted)">'
            f'{item["upvotes"]} upvotes · {html.escape(str(item["created_at"] or ""))[:19]}{reason}</div>'
            f'{("<div style=font-size:11px;color:var(--text-muted);margin-top:4px>Note: " + note_preview + "</div>") if note_preview else ""}'
            f'</div>'
            f'<form method="post" action="/admin/feedback/{item["id"]}/status" '
            f'style="display:flex;gap:4px;align-items:center;justify-self:end">'
            f'<select name="status" style="font-size:12px;padding:4px 8px;background:var(--bg-raised);'
            f'border:1px solid var(--border);border-radius:4px;color:var(--text-primary)">'
            + "".join(
                f'<option value="{s}"{" selected" if s == item["status"] else ""}>{s}</option>'
                for s in sorted(VALID_STATUSES)
            )
            + f'</select>'
            f'<button type="submit" class="sb-btn sb-btn-outline" style="font-size:11px;padding:4px 8px">'
            f'Save</button></form>'
            f'</div>'
        )

    status_filters = []
    for s in ["", "open", "in_progress", "shipped", "declined", "dup"]:
        label = "All" if not s else s.replace("_", " ").title()
        active = ((s == filter_status) or (not s and not filter_status))
        qs = f"?status={s}" if s else ""
        cls = "fb-chip fb-chip-active" if active else "fb-chip"
        status_filters.append(
            f'<a class="{cls}" href="/admin/feedback{qs}">{html.escape(label)}</a>'
        )

    # ENHANCEMENT #6 — bulk status change bar. A single <form
    # id="af-bulk"> posts all checked ids + the chosen status to
    # /admin/feedback/bulk-status. Rendering a matching hidden <form>
    # element with `id="af-bulk"` lets the row checkboxes (with
    # `form="af-bulk"`) submit to it.
    bulk_bar = (
        '<form id="af-bulk" method="post" action="/admin/feedback/bulk-status" '
        'style="display:flex;gap:8px;align-items:center;padding:12px 16px;'
        'background:var(--bg-raised);border:1px solid var(--border);border-radius:8px;'
        'margin-top:14px">'
        '<span style="font-size:11px;font-weight:600;color:var(--text-muted);'
        'text-transform:uppercase;letter-spacing:0.05em">Bulk action</span>'
        '<select name="status" required style="font-size:12px;padding:6px 10px;'
        'background:var(--bg-base);border:1px solid var(--border);border-radius:4px;'
        'color:var(--text-primary)">'
        + "".join(f'<option value="{s}">{s}</option>' for s in sorted(VALID_STATUSES))
        + '</select>'
        '<button type="submit" class="sb-btn sb-btn-outline" '
        'style="font-size:12px;padding:6px 12px">Apply to selected</button>'
        '<span style="font-size:11px;color:var(--text-muted);margin-left:auto">'
        'Tick rows above, then pick a status.</span>'
        '</form>'
    )

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/feedback.html",
        page_title="Feedback triage",
        active_route="feedback",
        breadcrumb=[("Admin", "/admin"), ("Feedback", "/admin/feedback")],
        raw_rows="".join(rows_html) or (
            '<div style="padding:48px;text-align:center;color:var(--text-muted);font-size:13px">'
            'No feedback submitted yet.</div>'
        ),
        raw_status_filters="".join(status_filters),
        raw_bulk_bar=bulk_bar,
    )


@app.post("/admin/feedback/{item_id}/status", include_in_schema=False)
async def admin_feedback_status(
    request: Request, item_id: int,
    status: str = Form(...),
    admin_note: str = Form(""),
):
    admin = _require_admin(request)
    status_clean = _sanitize_status(status)
    if not status_clean:
        raise HTTPException(status_code=400, detail="Invalid status")
    note = (admin_note or "").strip()[:2000] or None

    with db.conn() as c:
        row = c.execute(
            "SELECT user_id, status FROM feedback_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Feedback item not found")
        if note is not None:
            c.execute(
                "UPDATE feedback_items SET status = ?, admin_note = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (status_clean, note, item_id),
            )
        else:
            c.execute(
                "UPDATE feedback_items SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (status_clean, item_id),
            )
    submitter_id = row["user_id"] if row["user_id"] != admin["user_id"] else None
    event = "shipped" if status_clean == "shipped" else "status"
    _notify_submitter(submitter_id, item_id, event, extra={"status": status_clean})

    return RedirectResponse("/admin/feedback", status_code=302)


@app.post("/admin/feedback/bulk-status", include_in_schema=False)
async def admin_feedback_bulk_status(request: Request):
    """ENHANCEMENT #6 — set the same status on N checked items at once.

    The triage page is a <form> per row (single save) PLUS a bulk form
    at the bottom with checkboxes bound via ``form="af-bulk"``. Posts
    land here as ``ids=1&ids=2&ids=3&status=shipped``.

    Silently skips any id that doesn't resolve — an attacker spraying
    random ids won't leak existence through response shape.
    """
    admin = _require_admin(request)
    form = await request.form()
    status_clean = _sanitize_status(form.get("status", ""))
    if not status_clean:
        raise HTTPException(status_code=400, detail="Invalid status")
    raw_ids = form.getlist("ids") if hasattr(form, "getlist") else []
    # Coerce to ints, drop anything that isn't.
    item_ids: list[int] = []
    for r in raw_ids:
        try:
            item_ids.append(int(r))
        except (TypeError, ValueError):
            continue
    if not item_ids:
        return RedirectResponse("/admin/feedback", status_code=302)

    event = "shipped" if status_clean == "shipped" else "status"
    updated = 0
    with db.conn() as c:
        for iid in item_ids:
            row = c.execute(
                "SELECT user_id FROM feedback_items WHERE id = ?",
                (iid,),
            ).fetchone()
            if not row:
                continue
            c.execute(
                "UPDATE feedback_items SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (status_clean, iid),
            )
            submitter_id = row["user_id"] if row["user_id"] != admin["user_id"] else None
            _notify_submitter(submitter_id, iid, event, extra={"status": status_clean})
            updated += 1
    log.info(
        "bulk status admin=%s -> status=%s count=%d",
        admin["user_id"], status_clean, updated,
    )
    return RedirectResponse("/admin/feedback", status_code=302)


@app.post("/admin/feedback/{item_id}/duplicate", include_in_schema=False)
async def admin_feedback_duplicate(request: Request, item_id: int, duplicate_of: int = Form(...)):
    admin = _require_admin(request)
    with db.conn() as c:
        row = c.execute("SELECT user_id FROM feedback_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Feedback item not found")
        tgt = c.execute("SELECT id FROM feedback_items WHERE id = ?", (duplicate_of,)).fetchone()
        if not tgt:
            raise HTTPException(status_code=400, detail="Target duplicate does not exist")
        c.execute(
            "UPDATE feedback_items SET status = 'dup', duplicate_of = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (duplicate_of, item_id),
        )
    _notify_submitter(
        row["user_id"] if row["user_id"] != admin["user_id"] else None,
        item_id, "status", extra={"status": "dup", "duplicate_of": duplicate_of},
    )
    return RedirectResponse("/admin/feedback", status_code=302)


@app.post("/admin/feedback/{item_id}/comment", include_in_schema=False)
async def admin_feedback_comment(request: Request, item_id: int, body: str = Form(...)):
    admin = _require_admin(request)
    body_clean = (body or "").strip()[:2000]
    if not body_clean:
        raise HTTPException(status_code=400, detail="Comment body required")
    with db.conn() as c:
        row = c.execute("SELECT user_id FROM feedback_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Feedback item not found")
        c.execute(
            "INSERT INTO feedback_comments (feedback_id, user_id, body, is_admin) "
            "VALUES (?, ?, ?, 1)",
            (item_id, admin["user_id"], body_clean),
        )
        c.execute(
            "UPDATE feedback_items SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (item_id,),
        )
    _notify_submitter(
        row["user_id"] if row["user_id"] != admin["user_id"] else None,
        item_id, "admin_reply",
        extra={"excerpt": body_clean[:120]},
    )
    return RedirectResponse(f"/feedback/{item_id}", status_code=302)


@app.post("/admin/feedback/{item_id}/ship", include_in_schema=False)
async def admin_feedback_ship(request: Request, item_id: int, sha: str = Form("")):
    admin = _require_admin(request)
    sha_clean = (sha or "").strip()[:64] or None
    with db.conn() as c:
        row = c.execute("SELECT user_id FROM feedback_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Feedback item not found")
        c.execute(
            "UPDATE feedback_items SET status = 'shipped', shipped_commit_sha = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (sha_clean, item_id),
        )
    _notify_submitter(
        row["user_id"] if row["user_id"] != admin["user_id"] else None,
        item_id, "shipped", extra={"sha": sha_clean or ""},
    )
    return RedirectResponse(f"/feedback/{item_id}", status_code=302)
