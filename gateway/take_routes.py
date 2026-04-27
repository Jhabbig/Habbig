"""HTTP routes for the Community Takes feature.

Mount points (all live under the existing auth + CSRF middlewares, which
are registered on `server.app` before this module imports):

  GET    /api/v1/markets/{slug}/takes       list takes for a market
  POST   /api/v1/markets/{slug}/takes       create a take (paid-only)
  PATCH  /api/v1/takes/{id}                 edit own take (24h window)
  DELETE /api/v1/takes/{id}                 soft-delete own take
  POST   /api/v1/takes/{id}/vote            cast / replace a vote
  DELETE /api/v1/takes/{id}/vote            clear a vote
  POST   /api/v1/takes/{id}/report          flag for moderation

  GET    /settings/takes                    user's own take history (HTML)
  GET    /admin/moderation                  admin report queue (HTML)
  POST   /api/v1/admin/takes/{id}/delete    admin hard-delete
  POST   /api/v1/admin/reports/{id}/resolve admin resolves a report
"""

from __future__ import annotations

import html as _html
import logging
import time
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import db
import db_takes
import server
from server import app, current_user, render_page


log = logging.getLogger("take_routes")


# ── Paid-gate helper ───────────────────────────────────────────────────────


def _is_paid(user: dict) -> bool:
    """True if the user has any active paid entitlement.

    Sources, in order:
      1. Admins (and dev bypass) — always allowed.
      2. `db.get_user_subscription_tier(user_id)` → pro/trader/enterprise.
      3. Active subproduct subscription (sports/crypto/etc.) — fallback for
         users who bought a single subproduct without a full platform tier.
    """
    if not user:
        return False
    if user.get("is_admin") or user.get("_dev_bypass"):
        return True
    uid = user.get("user_id")
    if not uid:
        return False
    tier = "none"
    try:
        tier = (db.get_user_subscription_tier(uid) or "none").lower()
    except Exception:  # pragma: no cover
        tier = "none"
    if tier in ("pro", "trader", "enterprise"):
        return True
    # Any active subproduct ≫ counts as paid.
    try:
        subs = db.list_subscriptions(uid) or []
    except Exception:
        subs = []
    for s in subs:
        status = (s["status"] if "status" in s.keys() else "") or ""
        if str(status).lower() == "active":
            return True
    return False


def _require_paid(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    if not _is_paid(user):
        raise HTTPException(
            status_code=402,
            detail="Paid subscription required to post takes",
        )
    return user


def _require_admin(request: Request) -> dict:
    user = current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Serialisation ──────────────────────────────────────────────────────────


_POSITION_LABEL = {"yes": "YES", "no": "NO", "neutral": "Neutral"}


def _author_handle(user_id: Optional[int]) -> str:
    """Display handle for a take author; empty if the user row is gone."""
    if not user_id:
        return ""
    try:
        u = db.get_user_by_id(user_id)
    except Exception:
        return ""
    if not u:
        return ""
    try:
        username = u["username"] or ""
    except (KeyError, IndexError):
        username = ""
    if username:
        return username
    try:
        email = u["email"] or ""
    except (KeyError, IndexError):
        email = ""
    # Best-effort fallback: first chunk of email.
    return email.split("@")[0] if email else f"user{user_id}"


def _take_to_dict(
    take: Any,
    *,
    viewer_user_id: Optional[int] = None,
    viewer_vote: Optional[int] = None,
    author_cred: Optional[float] = None,
) -> dict:
    """Flat dict for JSON responses. Safe to render directly in templates.

    `viewer_user_id` / `viewer_vote` are extra context so the client can
    show the edit button or current vote state without a follow-up query.
    """
    author_id = take["user_id"]
    if author_cred is None and author_id is not None:
        # Blended = 0.85·global accuracy + 0.15·take accuracy. The small
        # take nudge stays SEPARATE from the global credibility score so
        # it never inflates the predictions-engine signal — see the
        # docstring on db_takes.get_blended_credibility.
        author_cred = db_takes.get_blended_credibility(author_id)

    return {
        "id": int(take["id"]),
        "user_id": author_id,
        "author_handle": _author_handle(author_id),
        "author_credibility": round(float(author_cred or 0.0), 3),
        "market_slug": take["market_slug"],
        "position": take["position"],
        "position_label": _POSITION_LABEL.get(take["position"], take["position"]),
        "confidence": take["confidence"],
        "reasoning": take["reasoning"],
        "created_at": int(take["created_at"] or 0),
        "edited_at": int(take["edited_at"] or 0) if take["edited_at"] else None,
        "upvotes": int(take["upvotes"] or 0),
        "downvotes": int(take["downvotes"] or 0),
        "quality_score": (
            float(take["quality_score"])
            if take["quality_score"] is not None else None
        ),
        "resolved_correct": (
            int(take["resolved_correct"])
            if take["resolved_correct"] is not None else None
        ),
        "shadow_hidden": bool(int(take["shadow_hidden"] or 0)),
        "can_edit": (
            viewer_user_id is not None
            and viewer_user_id == author_id
            and db_takes.can_edit(take)
        ),
        "is_own": viewer_user_id is not None and viewer_user_id == author_id,
        "viewer_vote": viewer_vote,
    }


# ── JSON helpers ───────────────────────────────────────────────────────────


async def _parse_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return {}
    return body


# ── GET /api/v1/markets/{slug}/takes ──────────────────────────────────────


@app.get("/api/v1/markets/{slug}/takes")
async def api_list_takes(slug: str, request: Request):
    """Public endpoint: anyone can READ takes. Posting is paid-only."""
    user = current_user(request)
    viewer_id = user["user_id"] if user else None

    pos_filter = (request.query_params.get("position") or "").strip().lower() or None
    sort = (request.query_params.get("sort") or "quality").strip().lower()
    try:
        limit = max(1, min(200, int(request.query_params.get("limit") or 100)))
        offset = max(0, int(request.query_params.get("offset") or 0))
    except ValueError:
        limit, offset = 100, 0

    takes = db_takes.list_market_takes(
        slug,
        viewer_user_id=viewer_id,
        position_filter=pos_filter,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    total = db_takes.count_market_takes(
        slug, viewer_user_id=viewer_id, position_filter=pos_filter,
    )

    # One DB round-trip for all of the viewer's current votes on this
    # market — avoids an N+1 on get_user_vote per take.
    viewer_votes = (
        db_takes.get_user_votes_for_market(slug, viewer_id)
        if viewer_id else {}
    )

    out = [
        _take_to_dict(
            t,
            viewer_user_id=viewer_id,
            viewer_vote=viewer_votes.get(int(t["id"])),
        )
        for t in takes
    ]
    return JSONResponse({
        "takes": out,
        "total": total,
        "can_post": _is_paid(user) if user else False,
        "sort": sort,
        "position_filter": pos_filter,
    })


# ── POST /api/v1/markets/{slug}/takes ─────────────────────────────────────


@app.post("/api/v1/markets/{slug}/takes")
async def api_create_take(slug: str, request: Request):
    """Create a take on a market.

    Hardened two ways:

    1. Input hygiene — ``position`` and ``reasoning`` flow through
       ``clean_text`` so NFC-normalised unicode, zero-width / bidi
       stripping, and null-byte rejection happen before the row lands
       in ``takes``. Length caps match the column definitions in
       migration 122 (``position`` = 64 chars, ``reasoning`` = 2000).
    2. Idempotency — a double-click or retry within 10 s collapses
       into a single row. Key on the ``Idempotency-Key`` header, or
       fingerprint on (slug, position, reasoning) when absent. Without
       this protection a flaky network + enthusiastic user-click
       duplicates takes in the same market, which then show as two
       separate rows on the market detail page.
    """
    user = _require_paid(request)
    body = await _parse_json(request)

    from security.input_hygiene import clean_text
    from security.idempotency import with_idempotency

    position = clean_text(
        body.get("position"), max_len=64, required=True, field="position",
    )
    reasoning = clean_text(
        body.get("reasoning"), max_len=2000, required=False, field="reasoning",
    ) or ""
    confidence = body.get("confidence")

    uid = user["user_id"]

    async def _do_create() -> dict:
        try:
            take_id = db_takes.create_take(
                user_id=uid,
                market_slug=slug,
                position=position,
                confidence=confidence,
                reasoning=reasoning,
            )
        except ValueError as e:
            # Surface DB validation failures as 400 to the caller.
            return {"_status": 400, "_detail": str(e)}
        take = db_takes.get_take(take_id)
        return {
            "_status": 201,
            "take": _take_to_dict(take, viewer_user_id=uid, viewer_vote=None),
        }

    result = await with_idempotency(
        user_id=uid,
        op=f"take_create:{slug}",
        client_key=request.headers.get("Idempotency-Key"),
        ttl_seconds=10,
        body=_do_create,
        fallback_fingerprint=f"{slug}|{position}|{reasoning}",
    )
    status = int(result.pop("_status", 201))
    if status != 201:
        raise HTTPException(status_code=status, detail=result.get("_detail") or "bad request")
    return JSONResponse(result["take"], status_code=201)


# ── PATCH /api/v1/takes/{id} ──────────────────────────────────────────────


@app.patch("/api/v1/takes/{take_id}")
async def api_update_take(take_id: int, request: Request):
    user = _require_paid(request)
    body = await _parse_json(request)

    # Route title + body through clean_text so edits get the same
    # unicode / control-char guards that creates do. Both fields are
    # optional on an update (PATCH semantics) — the clean_text call is
    # skipped entirely when the key isn't present in the body.
    from security.input_hygiene import clean_text
    position = (
        clean_text(body.get("position"), max_len=64, field="position")
        if "position" in body else None
    )
    reasoning = (
        clean_text(body.get("reasoning"), max_len=2000, field="reasoning",
                   allow_empty=True)
        if "reasoning" in body else None
    )

    try:
        ok, err = db_takes.update_take(
            take_id,
            user["user_id"],
            position=position,
            confidence=body.get("confidence") if "confidence" in body else None,
            reasoning=reasoning,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        # 404 for "not found"; 403 for "not owner"; 409 for "edit window closed".
        if err and err.startswith("edit window"):
            raise HTTPException(status_code=409, detail=err)
        if err == "not the owner":
            raise HTTPException(status_code=403, detail=err)
        raise HTTPException(status_code=404, detail=err or "take not found")
    take = db_takes.get_take(take_id)
    return _take_to_dict(take, viewer_user_id=user["user_id"])


# ── DELETE /api/v1/takes/{id} ─────────────────────────────────────────────


@app.delete("/api/v1/takes/{take_id}")
async def api_delete_take(take_id: int, request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    ok = db_takes.delete_take(take_id, user["user_id"])
    if not ok:
        raise HTTPException(status_code=404, detail="take not found")
    return {"deleted": True, "id": take_id}


# ── POST / DELETE /api/v1/takes/{id}/vote ────────────────────────────────


@app.post("/api/v1/takes/{take_id}/vote")
async def api_vote_on_take(take_id: int, request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    body = await _parse_json(request)
    try:
        vote = int(body.get("vote", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="vote must be +1 or -1")
    if vote == 0:
        up, down = db_takes.clear_vote(take_id, user["user_id"])
        return {"upvotes": up, "downvotes": down, "viewer_vote": None}
    try:
        up, down = db_takes.cast_vote(take_id, user["user_id"], vote)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # cast_vote no-ops for self-votes; viewer_vote reflects actual stored vote.
    return {
        "upvotes": up,
        "downvotes": down,
        "viewer_vote": db_takes.get_user_vote(take_id, user["user_id"]),
    }


@app.delete("/api/v1/takes/{take_id}/vote")
async def api_clear_vote(take_id: int, request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    up, down = db_takes.clear_vote(take_id, user["user_id"])
    return {"upvotes": up, "downvotes": down, "viewer_vote": None}


# ── POST /api/v1/takes/{id}/report ───────────────────────────────────────


_REPORT_REASONS = {"spam", "harassment", "misinformation", "off_topic", "other"}


@app.post("/api/v1/takes/{take_id}/report")
async def api_report_take(take_id: int, request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    take = db_takes.get_take(take_id)
    if not take:
        raise HTTPException(status_code=404, detail="take not found")
    if take["user_id"] == user["user_id"]:
        raise HTTPException(status_code=400, detail="cannot report your own take")
    body = await _parse_json(request)
    reason = (body.get("reason") or "").strip().lower()
    if reason not in _REPORT_REASONS:
        raise HTTPException(
            status_code=400,
            detail=f"reason must be one of {sorted(_REPORT_REASONS)}",
        )
    # Route free-form "details" through clean_text. Reports land on the
    # admin triage queue rendered as HTML — without unicode normalisation
    # a report containing zero-width joiners looks different from an
    # identical one submitted twice, breaking dedupe.
    from security.input_hygiene import clean_text
    details = clean_text(
        body.get("details"), max_len=1000, field="details", allow_empty=True,
    )

    try:
        rid = db_takes.create_report(
            take_id=take_id,
            reporter_user_id=user["user_id"],
            reason=reason,
            details=details,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"report_id": rid, "status": "received"}


# ── /settings/takes — user history page ──────────────────────────────────


@app.get("/settings/takes", response_class=HTMLResponse)
async def settings_takes_page(request: Request):
    user = current_user(request)
    if not user:
        return server.redirect_to_login(request) if hasattr(
            server, "redirect_to_login"
        ) else HTMLResponse("<h1>401 — login required</h1>", status_code=401)

    stats = db_takes.user_take_stats(user["user_id"])
    takes = db_takes.list_user_takes(user["user_id"], limit=200)
    rows_html = _render_user_takes_rows(takes)

    correct_rate_str = (
        f"{stats['correct_rate'] * 100:.0f}%"
        if stats["correct_rate"] is not None else "—"
    )
    avg_q_str = (
        f"{stats['avg_quality']:.1f}"
        if stats["avg_quality"] is not None else "—"
    )

    _username = user.get("email", "").split("@")[0]
    _role = server._role_badge(user) if hasattr(server, "_role_badge") else ""
    _admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    try:
        from sidebar import render_sidebar as _render_sidebar
        _sidebar_html = _render_sidebar(
            request, active="settings",
            username=_username,
            raw_admin_link=_admin_link,
            raw_nav_role=_role,
        )
    except Exception:
        _sidebar_html = ""

    return render_page(
        "settings_takes",
        request=request,
        raw_nav_role=_role,
        username=_username,
        raw_sidebar=_sidebar_html,
        total_takes=str(stats["total"]),
        correct_count=str(stats["correct"]),
        incorrect_count=str(stats["incorrect"]),
        unresolved_count=str(stats["unresolved"]),
        correct_rate=correct_rate_str,
        avg_quality=avg_q_str,
        raw_takes_rows=rows_html,
    )


def _render_user_takes_rows(takes: list) -> str:
    if not takes:
        return (
            '<tr><td colspan="5" style="padding:24px;text-align:center;'
            'color:var(--text-tertiary)">No takes yet. Post your first one '
            'on any market page.</td></tr>'
        )
    out: list[str] = []
    for t in takes:
        slug = _html.escape(t["market_slug"] or "")
        pos = _html.escape(_POSITION_LABEL.get(t["position"], t["position"]))
        reasoning = _html.escape((t["reasoning"] or "")[:120])
        votes = f"▲{int(t['upvotes'] or 0)} ▼{int(t['downvotes'] or 0)}"
        status = "—"
        if t["resolved_correct"] == 1:
            status = '<span style="color:var(--semantic-high)">correct ✓</span>'
        elif t["resolved_correct"] == 0:
            status = '<span style="color:var(--semantic-low)">incorrect ✗</span>'
        elif t["shadow_hidden"]:
            status = '<span style="color:var(--text-tertiary)">hidden</span>'
        out.append(
            f'<tr>'
            f'<td><a href="/markets/{slug}" style="text-decoration:none;color:var(--text-primary)">{slug}</a></td>'
            f'<td>{pos}</td>'
            f'<td style="max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{reasoning}</td>'
            f'<td style="font-family:var(--font-mono)">{votes}</td>'
            f'<td>{status}</td>'
            f'</tr>'
        )
    return "".join(out)


# ── /admin/moderation + admin actions ─────────────────────────────────────


@app.get("/admin/moderation", response_class=HTMLResponse)
async def admin_moderation_page(request: Request):
    user = _require_admin(request)
    reports = db_takes.list_open_reports(limit=200)
    rows_html = _render_report_rows(reports)
    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/moderation.html",
        page_title="Moderation queue",
        active_route="moderation",
        breadcrumb=[("Admin", "/admin"), ("Moderation", "/admin/moderation")],
        open_report_count=str(len(reports)),
        raw_report_rows=rows_html,
    )


def _render_report_rows(reports: list) -> str:
    if not reports:
        return (
            '<tr><td colspan="6" style="padding:24px;text-align:center;'
            'color:var(--text-tertiary)">Queue is clear. Nothing to review.</td></tr>'
        )
    out: list[str] = []
    for r in reports:
        rid = int(r["id"])
        take_id = int(r["take_id"])
        reason = _html.escape(r["reason"] or "")
        preview = _html.escape((r["take_reasoning"] or "")[:120])
        slug = _html.escape(r["take_market_slug"] or "")
        reporter = int(r["reporter_user_id"] or 0)
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(r["reported_at"] or 0)))
        state = ""
        if r["take_deleted"]:
            state = (
                '<span style="color:var(--text-tertiary);font-size:11px">'
                'take already deleted</span>'
            )
        # Action cell wraps flex so buttons can break onto a second line
        # on narrow viewports without piling over the reporter column.
        # Reporter cell uses font-mono so user IDs are scan-friendly.
        out.append(
            f'<tr data-report-id="{rid}" data-take-id="{take_id}">'
            f'<td>{when}</td>'
            f'<td><code>{reason}</code>{state}</td>'
            f'<td><a href="/markets/{slug}#take-{take_id}">{slug}</a></td>'
            f'<td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{preview}</td>'
            f'<td style="font-family:var(--font-mono);font-variant-numeric:tabular-nums">u{reporter}</td>'
            f'<td>'
            f'<div style="display:flex;flex-wrap:wrap;gap:6px">'
            f'<button class="btn btn-danger mod-act" data-action="deleted" data-rid="{rid}" data-tid="{take_id}" '
            f'        aria-label="Delete take {take_id} and close this report">Delete take</button>'
            f'<button class="btn mod-act" data-action="warned_user" data-rid="{rid}" data-tid="{take_id}" '
            f'        aria-label="Warn author of take {take_id}">Warn</button>'
            f'<button class="btn mod-act" data-action="dismissed" data-rid="{rid}" data-tid="{take_id}" '
            f'        aria-label="Dismiss this report">Dismiss</button>'
            f'</div>'
            f'</td>'
            f'</tr>'
        )
    return "".join(out)


@app.post("/api/v1/admin/takes/{take_id}/delete")
async def api_admin_delete_take(take_id: int, request: Request):
    admin = _require_admin(request)
    take = db_takes.get_take(take_id, include_deleted=True)
    if not take:
        raise HTTPException(status_code=404, detail="take not found")
    db_takes.admin_delete_take(take_id)
    resolved = db_takes.resolve_all_reports_for_take(
        take_id, admin_user_id=admin["user_id"], admin_action="deleted",
    )
    log.info(
        "admin.takes.delete take_id=%s admin=%s reports_closed=%d",
        take_id, admin["user_id"], resolved,
    )
    return {"deleted": True, "reports_closed": resolved}


@app.post("/api/v1/admin/reports/{report_id}/resolve")
async def api_admin_resolve_report(report_id: int, request: Request):
    admin = _require_admin(request)
    body = await _parse_json(request)
    action = (body.get("action") or "").strip()
    take_id = body.get("take_id")

    try:
        ok = db_takes.resolve_report(
            report_id, admin_user_id=admin["user_id"], admin_action=action,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="report not found or already resolved")

    # If the admin chose "deleted", also soft-delete the underlying take +
    # auto-resolve any sibling reports on the same take.
    extra = {}
    if action == "deleted" and take_id:
        db_takes.admin_delete_take(int(take_id))
        extra["take_deleted"] = True
        siblings = db_takes.resolve_all_reports_for_take(
            int(take_id), admin_user_id=admin["user_id"], admin_action="deleted",
        )
        extra["sibling_reports_closed"] = siblings

    log.info(
        "admin.reports.resolve report_id=%s admin=%s action=%s %s",
        report_id, admin["user_id"], action, extra,
    )
    return {"resolved": True, "report_id": report_id, "action": action, **extra}


# ── Public profile: /u/{user_id}/takes ─────────────────────────────────────


@app.get("/u/{user_id}/takes", response_class=HTMLResponse)
async def public_user_takes_page(user_id: int, request: Request):
    """Public-facing "best takes" strip for a user who has opted in.

    Re-uses the existing `users.leaderboard_participation` opt-in rather
    than adding a dedicated takes-only flag — a user who agreed to appear
    on the public leaderboard has already consented to a public identity
    on the site, and this avoids a new migration on top of 122–124.

    Shows up to 5 takes ranked by quality_score (strict filter: no
    shadow-hidden rows, no NULL scores). Returns 404 for anyone who
    hasn't opted in, so we don't leak the mere existence of an account.
    """
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")

    if not db_takes.user_opts_in_public_takes(user_id):
        # Don't reveal that the account exists — same pattern as
        # public_profile_page in user_prediction_routes.py.
        raise HTTPException(status_code=404, detail="Profile not public")

    best = db_takes.list_user_best_takes(user_id, limit=5)
    stats = db_takes.user_take_stats(user_id)
    blended = db_takes.get_blended_credibility(user_id)

    handle = _author_handle(user_id)
    correct_rate = (
        f"{stats['correct_rate'] * 100:.0f}%"
        if stats["correct_rate"] is not None else "—"
    )
    avg_q = (
        f"{stats['avg_quality']:.1f}"
        if stats["avg_quality"] is not None else "—"
    )

    # Render server-side — stay a small, cached, SEO-friendly page. Keep
    # the per-take markup isolated from market_takes.js to avoid pulling
    # the whole posting/voting JS onto the public profile.
    take_rows_html = _render_public_best_takes(best, stats["total"])

    return render_page(
        "public_user_takes",
        request=request,
        handle=handle,
        email_domain=(user["email"] or "").split("@")[-1] if user["email"] else "",
        blended_credibility=f"{blended:.2f}",
        total_takes=str(stats["total"]),
        correct_count=str(stats["correct"]),
        incorrect_count=str(stats["incorrect"]),
        correct_rate=correct_rate,
        avg_quality=avg_q,
        raw_best_takes=take_rows_html,
    )


def _render_public_best_takes(takes: list, total: int) -> str:
    if total == 0:
        return (
            '<div class="empty-state" style="padding:32px;text-align:center;'
            'color:var(--text-tertiary)">No takes yet.</div>'
        )
    if not takes:
        return (
            '<div class="empty-state" style="padding:32px;text-align:center;'
            'color:var(--text-tertiary)">No scored takes yet — check back '
            'after the next market resolution.</div>'
        )
    out: list[str] = []
    for t in takes:
        slug = _html.escape(t["market_slug"] or "")
        pos = _POSITION_LABEL.get(t["position"], t["position"])
        # Monochrome colour hints — no green/red, in line with the rest of
        # the dashboard palette.
        pos_tone = "var(--text-primary)" if t["position"] == "yes" else (
            "var(--text-secondary)" if t["position"] == "no"
            else "var(--text-tertiary)"
        )
        reasoning = _html.escape((t["reasoning"] or "")[:280])
        conf = f" · {t['confidence']}/10" if t["confidence"] else ""
        resolved = ""
        if t["resolved_correct"] == 1:
            resolved = (
                '<span style="font-size:11px;color:var(--semantic-high);'
                'margin-left:8px" title="Position matched outcome">✓ correct</span>'
            )
        elif t["resolved_correct"] == 0:
            resolved = (
                '<span style="font-size:11px;color:var(--semantic-low);'
                'margin-left:8px" title="Position did not match outcome">✗ incorrect</span>'
            )
        q = (
            f"{float(t['quality_score']):.1f}"
            if t["quality_score"] is not None else "—"
        )
        out.append(
            f'<article style="padding:16px 0;border-bottom:1px solid var(--border-ghost)">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline">'
            f'<div>'
            f'<a href="/markets/{slug}" style="text-decoration:none;color:inherit">'
            f'<strong style="color:{pos_tone}">{pos}</strong>{conf} on '
            f'<span style="color:var(--text-secondary)">{slug}</span></a>'
            f'{resolved}'
            f'</div>'
            f'<span style="font-size:11px;color:var(--text-tertiary);font-family:var(--font-mono)">'
            f'q {q}</span>'
            f'</div>'
            f'<p style="font-size:14px;line-height:1.5;margin:8px 0 0;color:var(--text-secondary)">'
            f'{reasoning}</p>'
            f'</article>'
        )
    return "".join(out)
