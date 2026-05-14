"""User-prediction routes — subscribers can log their own predictions on any
active market and have them scored on resolution.

Endpoints:
  POST  /api/predictions                      create
  PATCH /api/predictions/{prediction_id}      edit (probability/reasoning/visibility, direction locked)
  GET   /api/predictions/me                   the caller's history + stats (JSON)
  GET   /predictions                          HTML history + stats page
  GET   /predictions/{prediction_id}          public detail page
  GET   /predictions/public/{user_id}         opt-in public profile

Edit window: 24 hours from creation; direction (YES/NO) is always locked.
Enforcement lives here in the route layer so we don't need to mutate the
DB schema with a trigger.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
from sidebar import render_sidebar


log = logging.getLogger("gateway.user_prediction_routes")

EDIT_WINDOW_SECONDS = 24 * 60 * 60   # 24h from creation, direction-locked anyway

# Allowed categories match those used by the market taxonomy. "other" is
# the fallback when a market hasn't been categorised yet.
_CATEGORIES = {"sports", "weather", "world", "crypto", "midterm", "traders", "climate", "voters", "other"}


# ── Deferred lookups into server.py (admin_routes.py pattern) ─────────────


def _srv():
    return sys.modules.get("server") or sys.modules["__main__"]


def _current_user(request):
    return _srv().current_user(request)


def _render(name, request, **ctx):
    return _srv().render_page(name, request=request, **ctx)


def _role_badge(user):
    return _srv()._role_badge(user)


# ── Helpers ────────────────────────────────────────────────────────────


def _require_paid_user(request):
    """User must be signed in AND have at least one active subscription.

    Pulls tier via db.get_user_subscription_tier if available; falls back
    to the `subscription_tier` field on the user row. Anonymous or free
    users are blocked — user predictions are a subscriber feature.
    """
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")

    tier = "none"
    try:
        tier = db.get_user_subscription_tier(user["user_id"]) or "none"
    except Exception:
        pass
    if tier in (None, "none", "free"):
        raise HTTPException(status_code=402, detail="Subscription required")
    return user


def _within_edit_window(row) -> bool:
    return int(time.time()) - int(row["created_at"] or 0) <= EDIT_WINDOW_SECONDS


def _clamp_prob(value) -> float:
    try:
        p = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="predicted_probability must be a number")
    if not 0.0 <= p <= 1.0:
        # Accept 0-100 too so users pasting "72" don't trip over units.
        if 0.0 <= p <= 100.0:
            p = p / 100.0
        else:
            raise HTTPException(
                status_code=400,
                detail="predicted_probability must be between 0 and 1 (or 0 and 100)",
            )
    return p


def _prediction_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "user_id": r["user_id"],
        "market_slug": r["market_slug"],
        "market_question": r["market_question"],
        "category": r["category"],
        "predicted_outcome": r["predicted_outcome"],
        "predicted_probability": r["predicted_probability"],
        "reasoning": r["reasoning"],
        "created_at": r["created_at"],
        "market_price_at_prediction": r["market_price_at_prediction"],
        "edge_at_prediction": r["edge_at_prediction"],
        "is_public": bool(r["is_public"]),
        "is_anonymous": bool(r["is_anonymous"]),
        "resolved": bool(r["resolved"]),
        "resolved_at": r["resolved_at"],
        "resolved_correct": r["resolved_correct"],
        "final_market_price": r["final_market_price"],
        "brier_score": r["brier_score"],
        "timing_score": r["timing_score"],
    }


# ── API routes ─────────────────────────────────────────────────────────


async def api_create_prediction(
    request: Request,
    market_slug: str = Form(...),
    predicted_outcome: str = Form(...),
    predicted_probability: str = Form(...),
    market_question: str = Form(""),
    category: str = Form("other"),
    reasoning: str = Form(""),
    market_price: str = Form(""),
    is_public: str = Form(""),
    is_anonymous: str = Form(""),
):
    user = _require_paid_user(request)

    # Normalise + validate.
    slug = market_slug.strip()
    if not slug or len(slug) > 200:
        raise HTTPException(status_code=400, detail="market_slug required")
    outcome = predicted_outcome.strip().upper()
    if outcome not in ("YES", "NO"):
        raise HTTPException(status_code=400, detail="predicted_outcome must be YES or NO")
    prob = _clamp_prob(predicted_probability)
    cat = (category or "other").strip().lower()
    if cat not in _CATEGORIES:
        cat = "other"
    reasoning_clean = (reasoning or "").strip()[:4000]  # cap — no novels
    mq = (market_question or "").strip()[:500]

    market_price_value: Optional[float] = None
    if market_price:
        try:
            mpv = float(market_price)
            market_price_value = mpv / 100.0 if mpv > 1.0 else mpv
        except ValueError:
            market_price_value = None

    # Reject duplicate active prediction. `UNIQUE` index in the schema
    # covers this at write time, but checking first gives us a nicer
    # 409 instead of a 500 from sqlite.
    existing = db.get_active_user_prediction(user["user_id"], slug)
    if existing:
        raise HTTPException(status_code=409, detail="You already have an active prediction on this market")

    try:
        pid = db.create_user_prediction(
            user_id=user["user_id"],
            market_slug=slug,
            market_question=mq,
            category=cat,
            predicted_outcome=outcome,
            predicted_probability=prob,
            reasoning=reasoning_clean or None,
            market_price_at_prediction=market_price_value,
            is_public=bool(is_public),
            is_anonymous=bool(is_anonymous),
        )
    except Exception as exc:
        log.exception("create_user_prediction failed user=%d slug=%s: %s",
                      user["user_id"], slug, exc)
        raise HTTPException(status_code=500, detail="Could not save prediction")

    log.info("user_prediction created id=%d user=%d slug=%s outcome=%s p=%.3f",
             pid, user["user_id"], slug, outcome, prob)
    return JSONResponse({"prediction_id": pid, "status": "ok"}, status_code=201)


async def api_update_prediction(
    request: Request,
    prediction_id: int,
    predicted_probability: str = Form(""),
    reasoning: str = Form(""),
    is_public: str = Form(""),
):
    user = _require_paid_user(request)
    row = db.get_user_prediction(prediction_id)
    if not row:
        raise HTTPException(status_code=404, detail="Prediction not found")
    if row["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="Not your prediction")
    if row["resolved"]:
        raise HTTPException(status_code=409, detail="Cannot edit a resolved prediction")
    if not _within_edit_window(row):
        raise HTTPException(status_code=409, detail="Edit window has closed (24h from creation)")

    kwargs: dict = {}
    if predicted_probability:
        kwargs["predicted_probability"] = _clamp_prob(predicted_probability)
    if reasoning:
        kwargs["reasoning"] = reasoning.strip()[:4000]
    # Checkbox presence = public; empty string = private. Form semantics.
    if is_public != "":
        kwargs["is_public"] = bool(is_public)
    if not kwargs:
        raise HTTPException(status_code=400, detail="No fields to update")

    ok = db.update_user_prediction(prediction_id, **kwargs)
    if not ok:
        raise HTTPException(status_code=409, detail="Update failed (already resolved?)")
    return JSONResponse({"status": "ok"})


async def api_my_predictions(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    rows = db.list_user_predictions(user["user_id"], limit=500)
    stats = db.get_user_prediction_stats(user["user_id"])
    return JSONResponse({
        "predictions": [_prediction_to_dict(r) for r in rows],
        "stats": dict(stats) if stats else None,
    })


# ── Page routes ────────────────────────────────────────────────────────


def _fmt_prob(p) -> str:
    try:
        return f"{float(p) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _build_prediction_rows_html(rows) -> str:
    import datetime as _dt
    import html

    parts: list[str] = []
    for r in rows:
        created = _dt.datetime.fromtimestamp(int(r["created_at"]), tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        outcome = html.escape(r["predicted_outcome"] or "—")
        prob = _fmt_prob(r["predicted_probability"])
        q = html.escape((r["market_question"] or r["market_slug"] or "")[:140])

        if r["resolved"]:
            if r["resolved_correct"]:
                status = '<span class="badge" style="background:rgba(34,197,94,0.12);color:#22c55e">Correct</span>'
            else:
                status = '<span class="badge" style="background:rgba(239,68,68,0.12);color:#ef4444">Incorrect</span>'
        elif _within_edit_window(r):
            status = '<span class="badge" style="background:rgba(245,158,11,0.12);color:#f59e0b">Editable</span>'
        else:
            status = '<span class="badge" style="background:var(--surface-hover);color:var(--text-muted)">Active</span>'

        extras = []
        if r["brier_score"] is not None:
            extras.append(f'Brier {float(r["brier_score"]):.3f}')
        if r["edge_at_prediction"] is not None:
            extras.append(f'Edge {float(r["edge_at_prediction"]) * 100:.1f}%')
        if r["is_public"]:
            extras.append("Public")

        parts.append(
            '<div class="admin-row">'
            '<div class="admin-row-info">'
            f'<div class="admin-row-main">{outcome} @ {prob} · {q} {status}</div>'
            f'<div class="admin-row-meta">{created}{" · " + " · ".join(extras) if extras else ""}</div>'
            '</div></div>'
        )
    return "".join(parts)


async def predictions_history_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login?next=/predictions", status_code=302)

    rows = db.list_user_predictions(user["user_id"], limit=200)
    stats = db.get_user_prediction_stats(user["user_id"])

    if stats:
        total = int(stats["total_predictions"] or 0)
        resolved = int(stats["resolved_predictions"] or 0)
        correct = int(stats["correct_predictions"] or 0)
        accuracy_pct = (float(stats["accuracy"]) * 100) if stats["accuracy"] is not None else None
        avg_brier = stats["avg_brier_score"]
        streak = int(stats["current_streak"] or 0)
    else:
        total = len(rows)
        resolved = sum(1 for r in rows if r["resolved"])
        correct = sum(1 for r in rows if r["resolved_correct"])
        accuracy_pct = (correct / resolved * 100) if resolved else None
        avg_brier = None
        streak = 0

    summary_parts = [
        ("Total", str(total)),
        ("Resolved", str(resolved)),
        ("Correct", str(correct)),
        ("Accuracy", f"{accuracy_pct:.1f}%" if accuracy_pct is not None else "—"),
        ("Avg Brier", f"{avg_brier:.3f}" if avg_brier is not None else "—"),
        ("Streak", str(streak)),
    ]
    summary_html = "".join(
        f'<div class="stat-card"><div class="stat-label">{lbl}</div>'
        f'<div class="stat-value">{val}</div></div>'
        for lbl, val in summary_parts
    )

    nav_role = _role_badge(user)
    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    _sidebar = render_sidebar(
        request,
        active="predictions",
        username=user.get("username", user["email"]),
        raw_admin_link=admin_link,
        raw_nav_role=nav_role,
    )
    return _render(
        "predictions_history",
        request=request,
        email=user["email"],
        username=user.get("username", user["email"]),
        raw_nav_role=nav_role,
        raw_admin_link=admin_link,
        raw_summary_cards=summary_html,
        raw_prediction_rows=_build_prediction_rows_html(rows) or
            '<div class="admin-row"><div class="admin-row-info"><div class="admin-row-meta">'
            'No predictions yet. Visit any market to log one.'
            '</div></div></div>',
        raw_sidebar=_sidebar,
    )


async def prediction_detail_page(request: Request, prediction_id: int):
    row = db.get_user_prediction(prediction_id)
    if not row:
        raise HTTPException(status_code=404, detail="Prediction not found")

    user = _current_user(request)
    is_owner = bool(user and user["user_id"] == row["user_id"])

    if not row["is_public"] and not is_owner:
        # Private prediction, caller isn't the owner — don't leak existence.
        raise HTTPException(status_code=404, detail="Prediction not found")

    author = "Anonymous" if row["is_anonymous"] else None
    if not author:
        u = db.get_user_by_id(row["user_id"])
        author = (u["username"] or u["email"]) if u else "(deleted)"

    import datetime as _dt
    import html

    created = _dt.datetime.fromtimestamp(int(row["created_at"]), tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return _render(
        "user_prediction_detail",
        request=request,
        email=(user or {}).get("email", ""),
        username=(user or {}).get("username", ""),
        raw_nav_role=_role_badge(user) if user else "",
        prediction_id=str(prediction_id),
        author=author,
        created=created,
        market_question=row["market_question"] or row["market_slug"],
        predicted_outcome=row["predicted_outcome"] or "",
        probability=_fmt_prob(row["predicted_probability"]),
        reasoning=row["reasoning"] or "",
        resolved="Yes" if row["resolved"] else "No",
        resolved_correct=("—" if row["resolved_correct"] is None else
                          ("Correct" if row["resolved_correct"] else "Incorrect")),
        brier_score=(f'{float(row["brier_score"]):.3f}' if row["brier_score"] is not None else "—"),
        raw_owner_controls=("" if not is_owner else
            f'<form method="post" action="/api/predictions/{prediction_id}/toggle-public" '
            f'style="display:inline">'
            f'<button class="btn btn-primary-outline" style="font-size:12px">'
            f'{"Make private" if row["is_public"] else "Make public"}</button></form>'),
    )


async def public_profile_page(request: Request, user_id: int):
    u = db.get_user_by_id(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    rows = db.list_public_user_predictions(user_id, limit=100)
    if not rows:
        # Don't leak user existence for users who have no public predictions.
        raise HTTPException(status_code=404, detail="No public predictions")

    stats = db.get_user_prediction_stats(user_id)
    display_name = u["username"] or f"user{user_id}"

    summary = ""
    if stats:
        acc = f'{float(stats["accuracy"]) * 100:.1f}%' if stats["accuracy"] is not None else "—"
        summary = (
            f'<div class="stat-card"><div class="stat-label">Public predictions</div>'
            f'<div class="stat-value">{len(rows)}</div></div>'
            f'<div class="stat-card"><div class="stat-label">Accuracy</div>'
            f'<div class="stat-value">{acc}</div></div>'
            f'<div class="stat-card"><div class="stat-label">Avg Brier</div>'
            f'<div class="stat-value">{stats["avg_brier_score"] if stats["avg_brier_score"] is not None else "—"}</div></div>'
        )
    return _render(
        "user_prediction_profile",
        request=request,
        username=display_name,
        email=u["email"],
        raw_summary_cards=summary,
        raw_prediction_rows=_build_prediction_rows_html(rows),
    )


# ── Registration ───────────────────────────────────────────────────────


def register(app) -> None:
    app.add_api_route("/api/predictions", api_create_prediction,
                      methods=["POST"], include_in_schema=False)
    app.add_api_route("/api/predictions/{prediction_id}", api_update_prediction,
                      methods=["PATCH"], include_in_schema=False)
    app.add_api_route("/api/predictions/me", api_my_predictions,
                      methods=["GET"], include_in_schema=False)
    app.add_api_route("/predictions", predictions_history_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/predictions/public/{user_id}", public_profile_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/predictions/{prediction_id}", prediction_detail_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
