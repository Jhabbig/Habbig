"""Public profile (/u/{handle}) + opt-in settings + follow graph + OG card.

Wired from server.py via ``register(app)``. Adds ``/u/`` to the public
prefix list so unauthenticated visitors and crawlers can hit profile
pages — the server's gate middleware honours that.

Surface map:

    GET  /u/{handle}                        Public profile page
    GET  /og/profile/{handle}               1200×630 OG card (cached)
    GET  /settings/profile                  Settings tab — opt-in form
    POST /api/settings/profile              Update enabled / handle / bio
    POST /api/settings/avatar               Multipart upload (≤2 MB, webp out)
    DELETE /api/settings/avatar             Drop the avatar (gravatar fallback)
    POST /api/follow/{user_id}              Toggle follow; HTMX-aware

Validation, reserved-name handling, and the 30-day handle cooldown live
in ``queries.profile``. This module just plumbs HTTP → DB → template.
"""

from __future__ import annotations

import hashlib
import html as _html
import io
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import db
from queries import profile as profile_q
from queries import predictions as predictions_q
from sidebar import render_sidebar


log = logging.getLogger("gateway.profile_routes")

_STATIC = Path(__file__).parent / "static"
_AVATARS = _STATIC / "avatars"
_AVATARS.mkdir(exist_ok=True)

_MAX_AVATAR_BYTES = 2 * 1024 * 1024     # 2 MB
_AVATAR_SIZE = 200                      # 200×200
_OG_CARD_TTL = 600                      # 10 min — stats cache


def _srv():
    return sys.modules.get("server") or sys.modules["__main__"]


# ── Helpers ────────────────────────────────────────────────────────────


def _gravatar(email: str | None) -> str:
    """Default avatar via Gravatar's identicon fallback. Returned as a
    full https URL so it works inside OG cards and external scrapers."""
    h = hashlib.md5((email or "").strip().lower().encode("utf-8")).hexdigest()
    return f"https://www.gravatar.com/avatar/{h}?s=200&d=identicon"


def _avatar_url(user_row) -> str:
    """Resolve a user row to a public avatar URL."""
    direct = (user_row["profile_avatar_url"] if "profile_avatar_url" in user_row.keys() else None)
    if direct:
        return direct
    return _gravatar(user_row["email"] if "email" in user_row.keys() else "")


def _person_schema(user_row, stats: dict, *, profile_url: str) -> dict:
    """Build the schema.org Person JSON-LD payload for the profile head."""
    handle = user_row["profile_handle"]
    accuracy = stats.get("accuracy")
    accuracy_pct = f"{accuracy * 100:.0f}%" if accuracy is not None else "—"
    bio = (user_row["profile_bio"] if "profile_bio" in user_row.keys() else None) or ""
    return {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": f"@{handle}",
        "url": profile_url,
        "description": bio or f"Forecaster on narve.ai with {accuracy_pct} accuracy",
    }


def _stats_for_user(user_id: int) -> dict:
    """Pull cached stats from user_prediction_stats; tolerate missing rows."""
    row = predictions_q.get_user_prediction_stats(user_id)
    if not row:
        return {
            "total": 0,
            "resolved": 0,
            "correct": 0,
            "accuracy": None,
            "avg_brier": None,
            "current_streak": 0,
            "best_streak": 0,
        }
    return {
        "total": row["total_predictions"] or 0,
        "resolved": row["resolved_predictions"] or 0,
        "correct": row["correct_predictions"] or 0,
        "accuracy": row["accuracy"],
        "avg_brier": row["avg_brier_score"],
        "current_streak": row["current_streak"] or 0,
        "best_streak": row["best_streak"] or 0,
    }


def _strongest_category(user_id: int) -> tuple[str, str]:
    """Return (label, accuracy_text) of the user's highest-accuracy
    category with at least 5 resolved predictions. Falls back to ('—', '')
    when there aren't enough samples."""
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT category, "
                "       SUM(CASE WHEN resolved = 1 AND resolved_correct = 1 THEN 1 ELSE 0 END) AS correct, "
                "       SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) AS resolved "
                "FROM user_predictions "
                "WHERE user_id = ? AND is_public = 1 "
                "GROUP BY category "
                "HAVING resolved >= 5 "
                "ORDER BY (1.0 * correct / resolved) DESC LIMIT 1",
                (user_id,),
            ).fetchone()
    except Exception:
        return ("—", "")
    if not row or not row["resolved"]:
        return ("—", "")
    pct = row["correct"] / row["resolved"]
    return (str(row["category"] or "—"), f"{pct * 100:.0f}% over {row['resolved']}")


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_brier(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "—"


def _format_date(ts) -> str:
    import datetime as _dt
    if not ts:
        return "—"
    try:
        d = _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc)
        return d.strftime("%B %Y")
    except Exception:
        return "—"


def _follow_button_html(*, target_user_id: int, is_following: bool, follower_count: int) -> str:
    """Render the follow / unfollow button. HTMX swaps this in place
    on POST so we never need a page reload."""
    label = "Following" if is_following else "Follow"
    aria = "Unfollow" if is_following else "Follow"
    klass = "profile-follow profile-follow--active" if is_following else "profile-follow"
    return (
        f'<form class="{klass}" hx-post="/api/follow/{int(target_user_id)}" '
        f'hx-swap="outerHTML" hx-target="this" data-follow-form>'
        f'<button type="submit" aria-label="{aria}">{label}</button>'
        f'<span class="profile-follow__count" aria-live="polite">'
        f'{follower_count} follower{"" if follower_count == 1 else "s"}</span>'
        f'</form>'
    )


def _redirect_to_login(request: Request) -> RedirectResponse:
    nxt = request.url.path
    return RedirectResponse(f"/login?next={nxt}", status_code=302)


# ── /u/{handle} ────────────────────────────────────────────────────────


async def public_profile_page(request: Request, handle: str):
    handle = (handle or "").strip().lower()
    if not profile_q.HANDLE_RE.match(handle):
        raise HTTPException(status_code=404, detail="Profile not found")
    row = profile_q.get_profile_by_handle(handle)
    if not row:
        # Hide existence — never 403 here.
        raise HTTPException(status_code=404, detail="Profile not found")

    user_id = row["id"]
    stats = _stats_for_user(user_id)
    follower_n = profile_q.follower_count(user_id)

    # Recent public predictions (≤20). The query lives in queries.predictions.
    try:
        recent = predictions_q.list_public_user_predictions(user_id, limit=20)
    except Exception as exc:
        log.warning("profile: list_public_user_predictions failed for uid=%s: %s", user_id, exc)
        recent = []

    rows_html = []
    for p in recent[:20]:
        content = (p["content"] if "content" in p.keys() else None) or ""
        category = (p["category"] if "category" in p.keys() else None) or ""
        prob = p["predicted_probability"] if "predicted_probability" in p.keys() else None
        outcome = "—"
        if "resolved" in p.keys() and p["resolved"]:
            outcome = "✓" if p["resolved_correct"] else "✗"
        rows_html.append(
            f'<li class="prediction-row">'
            f'<div class="prediction-row__main">{_html.escape(content[:240])}</div>'
            f'<div class="prediction-row__meta">'
            f'<span>{_html.escape(category) or "—"}</span>'
            f'<span>{_fmt_pct(prob)}</span>'
            f'<span>{outcome}</span>'
            f'</div></li>'
        )
    raw_prediction_rows = "".join(rows_html) or (
        '<li class="prediction-row prediction-row--empty">'
        'No public predictions yet.</li>'
    )

    # Follow button: anonymous viewers see "Follow" CTA that goes to login.
    srv = _srv()
    viewer = srv.current_user(request) if hasattr(srv, "current_user") else None
    is_self = bool(viewer and viewer["user_id"] == user_id)
    if is_self:
        raw_follow_button = (
            '<a class="profile-follow profile-follow--self" href="/settings/profile">'
            'Edit profile</a>'
        )
    elif viewer:
        raw_follow_button = _follow_button_html(
            target_user_id=user_id,
            is_following=profile_q.is_following(viewer["user_id"], user_id),
            follower_count=follower_n,
        )
    else:
        raw_follow_button = (
            f'<a class="profile-follow" href="/login?next=/u/{_html.escape(handle)}">'
            f'Follow</a>'
            f'<span class="profile-follow__count">{follower_n} follower'
            f'{"" if follower_n == 1 else "s"}</span>'
        )

    strongest_name, strongest_acc = _strongest_category(user_id)
    bio = (row["profile_bio"] if "profile_bio" in row.keys() else None) or ""
    profile_url = f"https://narve.ai/u/{handle}"
    schema_payload = json.dumps(
        _person_schema(row, stats, profile_url=profile_url),
        separators=(",", ":"),
    ).replace("</", "<\\/")
    og_description = (
        bio if bio
        else f"Forecaster on narve.ai with {_fmt_pct(stats['accuracy'])} accuracy"
    )
    og_description_safe = _html.escape(og_description)
    handle_safe = _html.escape(handle)
    og_meta = (
        '<meta property="og:type" content="profile">\n'
        f'<meta property="og:title" content="@{handle_safe} on narve.ai">\n'
        f'<meta property="og:description" content="{og_description_safe}">\n'
        f'<meta property="og:url" content="{profile_url}">\n'
        f'<meta property="og:image" content="https://narve.ai/og/profile/{handle_safe}">\n'
        '<meta property="og:image:width" content="1200">\n'
        '<meta property="og:image:height" content="630">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:image" content="https://narve.ai/og/profile/{handle_safe}">'
    )

    # Hand off to render_page so we get the global head injection +
    # SEO + a11y plumbing for free.
    return srv.render_page(
        "profile_public",
        request=request,
        handle=handle,
        bio=bio,
        avatar_url=_avatar_url(row),
        accuracy_pct=_fmt_pct(stats["accuracy"]),
        resolved_count=str(stats["resolved"]),
        brier_score=_fmt_brier(stats["avg_brier"]),
        current_streak=str(stats["current_streak"]),
        strongest_category_name=strongest_name,
        strongest_category_accuracy=strongest_acc,
        created_date=_format_date(
            row["created_at"] if "created_at" in row.keys() else None
        ),
        follower_count=str(follower_n),
        raw_follow_button=raw_follow_button,
        raw_prediction_rows=raw_prediction_rows,
        raw_pagination="",
        raw_jsonld=f'<script type="application/ld+json">{schema_payload}</script>',
        raw_canonical=f'<link rel="canonical" href="{profile_url}">',
        raw_og=og_meta,
    )


# ── /og/profile/{handle} ───────────────────────────────────────────────


async def og_profile_card(handle: str):
    handle = (handle or "").strip().lower()
    if not profile_q.HANDLE_RE.match(handle):
        raise HTTPException(status_code=404)
    row = profile_q.get_profile_by_handle(handle)
    if not row:
        raise HTTPException(status_code=404)
    stats = _stats_for_user(row["id"])

    try:
        import og_cards
        cache_key = f"profile:{handle}:{stats.get('accuracy')}:{stats.get('total')}"
        accuracy_txt = (
            f"{stats['accuracy'] * 100:.0f}%"
            if stats.get("accuracy") is not None
            else "—"
        )
        brier_txt = (
            f"Brier {stats['avg_brier']:.3f}"
            if stats.get("avg_brier") is not None
            else f"{stats.get('total', 0)} predictions"
        )

        def _render() -> bytes:
            return og_cards._render(
                eyebrow=f"@{handle}",
                heading="Forecaster on narve.ai",
                stat_value=accuracy_txt,
                stat_label="Accuracy",
                footer=brier_txt,
            )

        data = og_cards.cached(cache_key, _OG_CARD_TTL, _render)
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": f"public, max-age={_OG_CARD_TTL}, s-maxage={_OG_CARD_TTL}"},
        )
    except Exception as exc:
        log.warning("og_profile_card failed for %s: %s", handle, exc)
        raise HTTPException(status_code=500, detail="OG card unavailable")


# ── /settings/profile + /api/settings/profile ──────────────────────────


async def settings_profile_page(request: Request):
    srv = _srv()
    user = srv.current_user(request) if hasattr(srv, "current_user") else None
    if not user:
        return _redirect_to_login(request)

    row = profile_q.get_profile_for_user(user["user_id"])
    if not row:
        raise HTTPException(status_code=404)

    enabled = bool(row["public_profile_enabled"])
    handle = row["profile_handle"] or ""
    bio = row["profile_bio"] or ""
    avatar_url = row["profile_avatar_url"] or _gravatar(row["email"])
    can_change_at = (row["profile_handle_changed_at"] or 0) + profile_q.HANDLE_CHANGE_COOLDOWN_SECS
    cooldown_left = max(0, can_change_at - int(time.time()))
    cooldown_msg = ""
    if cooldown_left and handle:
        days = cooldown_left // 86400 + 1
        cooldown_msg = (
            f'<p class="settings-cooldown">You can change your handle again in {days} day(s).</p>'
        )
    preview_link = ""
    if enabled and handle:
        preview_link = (
            f'<p class="settings-preview">'
            f'Live at <a href="/u/{_html.escape(handle)}">narve.ai/u/{_html.escape(handle)}</a></p>'
        )

    _username = user.get("username") or user.get("email", "").split("@")[0]
    _admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    _nav_role = srv._role_badge(user) if hasattr(srv, "_role_badge") else ""
    _sidebar = render_sidebar(
        request,
        active="settings",
        username=_username,
        raw_admin_link=_admin_link,
        raw_nav_role=_nav_role,
    )
    return srv.render_page(
        "settings_profile",
        request=request,
        username=_username,
        avatar_letter=(user.get("username") or user.get("email", "?"))[:1].upper(),
        raw_admin_link=_admin_link,
        raw_nav_role=_nav_role,
        _is_admin=user.get("is_admin"),
        avatar_url=avatar_url,
        checked_if_enabled="checked" if enabled else "",
        profile_handle=handle,
        profile_bio=bio,
        raw_cooldown_msg=cooldown_msg,
        raw_preview_link=preview_link,
        raw_sidebar=_sidebar,
    )


async def api_settings_profile(request: Request):
    srv = _srv()
    user = srv.current_user(request) if hasattr(srv, "current_user") else None
    if not user:
        return JSONResponse({"error": "auth_required"}, status_code=401)
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "bad_form"}, status_code=400)

    enabled = (form.get("public_profile_enabled") or "").lower() in ("1", "on", "true", "yes")
    handle = (form.get("profile_handle") or "").strip().lower() or None
    bio = (form.get("profile_bio") or "").strip() or None

    try:
        result = profile_q.update_profile(
            user["user_id"], enabled=enabled, handle=handle, bio=bio,
        )
    except profile_q.ProfileError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=400)

    log.info(
        "profile updated user_id=%s enabled=%s handle=%s",
        user["user_id"], enabled, result.get("profile_handle"),
    )
    return JSONResponse({"ok": True, **result})


# ── /api/settings/avatar ───────────────────────────────────────────────


async def api_settings_avatar(request: Request, file: Optional[UploadFile] = None):
    srv = _srv()
    user = srv.current_user(request) if hasattr(srv, "current_user") else None
    if not user:
        return JSONResponse({"error": "auth_required"}, status_code=401)

    # Accept either ``file=`` or any other multipart name; FastAPI only
    # binds named params explicitly.
    upload = file
    if upload is None:
        try:
            form = await request.form()
            for value in form.values():
                if hasattr(value, "filename") and value.filename:
                    upload = value
                    break
        except Exception:
            upload = None
    if upload is None or not upload.filename:
        return JSONResponse({"error": "no_file"}, status_code=400)

    raw = await upload.read()
    if len(raw) == 0:
        return JSONResponse({"error": "empty_file"}, status_code=400)
    if len(raw) > _MAX_AVATAR_BYTES:
        return JSONResponse(
            {"error": "too_large", "message": "Max 2 MB."},
            status_code=413,
        )

    try:
        from PIL import Image
    except ImportError:
        log.error("Pillow not installed — avatar upload disabled")
        return JSONResponse({"error": "server_misconfigured"}, status_code=500)

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()  # detect malformed images before we re-open for resize
        img = Image.open(io.BytesIO(raw))
    except Exception as exc:
        log.info("avatar reject (decode) user=%s: %s", user["user_id"], exc)
        return JSONResponse({"error": "bad_image"}, status_code=400)

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")

    # Centre-crop to square then resize to 200×200.
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.LANCZOS)

    out_path = _AVATARS / f"{user['user_id']}.webp"
    try:
        img.convert("RGB").save(out_path, format="WEBP", quality=85, method=4)
    except Exception as exc:
        log.warning("avatar write failed for user=%s: %s", user["user_id"], exc)
        return JSONResponse({"error": "write_failed"}, status_code=500)

    # Cache-bust query so the browser reloads the freshly-saved image.
    avatar_url = f"/_gateway_static/avatars/{user['user_id']}.webp?v={int(time.time())}"
    profile_q.update_avatar_url(user["user_id"], avatar_url)
    return JSONResponse({"ok": True, "avatar_url": avatar_url})


async def api_settings_avatar_delete(request: Request):
    srv = _srv()
    user = srv.current_user(request) if hasattr(srv, "current_user") else None
    if not user:
        return JSONResponse({"error": "auth_required"}, status_code=401)
    out_path = _AVATARS / f"{user['user_id']}.webp"
    try:
        if out_path.exists():
            out_path.unlink()
    except OSError as exc:
        log.warning("avatar delete failed user=%s: %s", user["user_id"], exc)
    profile_q.update_avatar_url(user["user_id"], None)
    fallback = _gravatar(
        (user.get("email") or db.get_user_by_id(user["user_id"])["email"]) or ""
    )
    return JSONResponse({"ok": True, "avatar_url": fallback})


# ── /api/follow/{user_id} ──────────────────────────────────────────────


async def api_toggle_follow(request: Request, user_id: int):
    srv = _srv()
    viewer = srv.current_user(request) if hasattr(srv, "current_user") else None
    if not viewer:
        return JSONResponse({"error": "auth_required"}, status_code=401)
    if viewer["user_id"] == user_id:
        return JSONResponse({"error": "self_follow"}, status_code=400)

    target = db.get_user_by_id(user_id)
    if not target:
        return JSONResponse({"error": "not_found"}, status_code=404)

    state = profile_q.toggle_follow(viewer["user_id"], user_id)

    # HTMX clients want HTML to swap in place; JSON callers (curl,
    # tests) want the structured payload. The Accept header — or HX-
    # Request — disambiguates.
    if (request.headers.get("hx-request", "").lower() == "true"
            or "text/html" in (request.headers.get("accept") or "").lower()):
        body = _follow_button_html(
            target_user_id=user_id,
            is_following=state["is_following"],
            follower_count=state["follower_count"],
        )
        return HTMLResponse(body)
    return JSONResponse(state)


# ── Registration ───────────────────────────────────────────────────────


def register(app) -> None:
    """Wire profile + follow + avatar routes."""
    from fastapi.responses import HTMLResponse as _HTMLResponse

    app.add_api_route(
        "/u/{handle}", public_profile_page,
        methods=["GET"], response_class=_HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/og/profile/{handle}", og_profile_card,
        methods=["GET"], include_in_schema=False,
    )
    app.add_api_route(
        "/settings/profile", settings_profile_page,
        methods=["GET"], response_class=_HTMLResponse, include_in_schema=False,
    )
    app.add_api_route(
        "/api/settings/profile", api_settings_profile,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/api/settings/avatar", api_settings_avatar,
        methods=["POST"], include_in_schema=False,
    )
    app.add_api_route(
        "/api/settings/avatar", api_settings_avatar_delete,
        methods=["DELETE"], include_in_schema=False,
    )
    app.add_api_route(
        "/api/follow/{user_id}", api_toggle_follow,
        methods=["POST"], include_in_schema=False,
    )
