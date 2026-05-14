"""Embed-widget routes — token-gated, domain-locked iframe widgets.

Kept in its own module (like ``server_features``) so the monster
``server.py`` stays compact. Imported from ``server.py`` after the
helpers exist but *before* the catch-all registers its
``/{full_path:path}`` route; otherwise the catch-all would shadow
``/embed/{widget_id}`` and every ``/api/embeds/*`` endpoint.

Data model: ``migrations/021_embed_widgets.py``.
Token signing: ``embed_tokens.py``.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import embed_tokens
from server import (
    app,
    render_page,
    current_user,
    get_subdomain,
    proxy_request,
    _require_authenticated,
    _role_badge,
    EMBED_CSP_DEFAULT,
)
from sidebar import render_sidebar


log = logging.getLogger("embed_routes")


_EMBED_APP_URL = os.environ.get("APP_URL", "https://narve.ai").rstrip("/")
# RFC-1123-ish bare hostname: two or more labels, each 1–63 chars, a–z/0–9
# with optional internal hyphens. Rejects schemes, paths, ports.
_DOMAIN_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)


# ── Auth helpers ────────────────────────────────────────────────────────────


def _require_paid_user(request: Request) -> dict:
    """Accept any active paid subscription (not just ``plan == "pro"``).

    Embed widgets aren't scoped to a single dashboard the way the
    intelligence / environmental-impact features are. Any paying narve.ai
    customer qualifies. Admins always pass.
    """
    user = _require_authenticated(request)
    if user.get("is_admin"):
        return user
    if not db.has_any_active_subscription(user["user_id"]):
        raise HTTPException(status_code=403, detail="Active subscription required")
    return user


# ── Serialisation helpers ───────────────────────────────────────────────────


def _widget_to_api_dict(w, *, include_token: bool = False) -> dict:
    """Shape an ``embed_widgets`` row for JSON output.

    The signed token + iframe snippet is only included when the caller
    owns the widget (create / list / rotate paths). Keeps the boundary
    explicit even though no endpoint currently returns widgets across
    users.
    """
    out = {
        "widget_id": w["widget_id"],
        "widget_type": w["widget_type"],
        "target": w["target"],
        "domain": w["domain"],
        "theme": w["theme"],
        "is_active": bool(w["is_active"]),
        "created_at": w["created_at"],
        "last_used_at": w["last_used_at"],
        "impressions": w["impressions"],
    }
    if include_token:
        token = embed_tokens.sign(w["widget_id"], w["token_salt"])
        iframe_src = f"{_EMBED_APP_URL}/embed/{w['widget_id']}?token={token}"
        out["embed_token"] = token
        out["iframe_src"] = iframe_src
        out["embed_code"] = (
            f'<iframe src="{iframe_src}" '
            f'width="400" height="200" frameborder="0" '
            f'style="border:1px solid #e0e0e0;border-radius:8px"></iframe>'
        )
    return out


# ── Inline CSS + error page ─────────────────────────────────────────────────


def _embed_css() -> str:
    """Inline monochrome CSS for the iframe body. No external requests."""
    return (
        ":root{--fg:#000;--fg-2:#555;--fg-3:#999;--bg:#fff;"
        "--bd:#e0e0e0;--rule:#f0f0f0}"
        "[data-theme=dark]{--fg:#fff;--fg-2:#aaa;--fg-3:#666;"
        "--bg:#0a0a0a;--bd:#1f1f1f;--rule:#141414}"
        "@media (prefers-color-scheme:dark){"
        ":root:not([data-theme=light]){--fg:#fff;--fg-2:#aaa;"
        "--fg-3:#666;--bg:#0a0a0a;--bd:#1f1f1f;--rule:#141414}}"
        "*{box-sizing:border-box}"
        "html,body{margin:0;padding:0;height:100%}"
        ".ew-body{font:13px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;"
        "color:var(--fg);background:var(--bg);padding:18px 20px;"
        "display:flex;flex-direction:column;min-height:100vh}"
        ".ew-root{flex:1}"
        ".ew-handle{font-weight:600;font-size:15px;margin-bottom:10px}"
        ".ew-score{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        "font-size:28px;font-weight:400;letter-spacing:-0.02em;margin-top:4px}"
        ".ew-label{font-size:11px;letter-spacing:0.08em;"
        "text-transform:uppercase;color:var(--fg-3);margin-top:2px}"
        ".ew-rule{border:none;border-top:1px solid var(--rule);margin:12px 0}"
        ".ew-subtitle{font-size:11px;color:var(--fg-3);margin-bottom:4px}"
        ".ew-quote{font-size:13px;color:var(--fg-2);font-style:italic}"
        ".ew-question{font-size:13px;font-weight:500;margin-bottom:12px}"
        ".ew-dl{margin:0;padding:0}"
        ".ew-dl>div{display:flex;justify-content:space-between;"
        "padding:4px 0;font-size:13px}"
        ".ew-dl dt{color:var(--fg-3);margin:0}"
        ".ew-dl dd{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        "color:var(--fg);margin:0;font-weight:500}"
        ".ew-edge{margin-top:10px;padding-top:10px;"
        "border-top:1px solid var(--rule);font-size:12px;color:var(--fg-2)}"
        ".ew-eyebrow{font-size:11px;letter-spacing:0.08em;"
        "text-transform:uppercase;color:var(--fg-3);margin-bottom:10px}"
        ".ew-picks{list-style:none;counter-reset:pk;margin:0;padding:0}"
        ".ew-picks li{counter-increment:pk;padding:8px 0;"
        "border-bottom:1px solid var(--rule)}"
        ".ew-picks li:last-child{border-bottom:none;padding-bottom:0}"
        ".ew-picks li::before{content:counter(pk) \". \";"
        "color:var(--fg-3);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        "font-size:11px}"
        ".ew-pick-q{font-size:13px;color:var(--fg)}"
        ".ew-pick-meta{font-size:11px;color:var(--fg-3);margin-top:2px;"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace}"
        ".ew-empty{font-size:12px;color:var(--fg-3);font-style:italic}"
        ".ew-powered{display:block;font-size:10px;color:var(--fg-3);"
        "margin-top:14px;text-decoration:none;letter-spacing:0.04em}"
        ".ew-powered strong{color:var(--fg-2);font-weight:500}"
        ".ew-powered:hover strong{color:var(--fg)}"
    )


def _render_embed_error(message: str) -> HTMLResponse:
    """Minimal iframe-safe error card. Used for every failure path."""
    safe = html.escape(message)
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Widget unavailable</title><style>"
        + _embed_css() +
        ".ew-err{display:flex;align-items:center;justify-content:center;"
        "min-height:100%;text-align:center}"
        ".ew-err-inner{max-width:320px}"
        ".ew-err-title{font-size:11px;letter-spacing:0.08em;"
        "text-transform:uppercase;color:var(--fg-3);margin-bottom:6px}"
        ".ew-err-msg{font-size:13px;color:var(--fg-2)}"
        "</style></head>"
        "<body class='ew-body'><div class='ew-err'><div class='ew-err-inner'>"
        "<div class='ew-err-title'>narve.ai</div>"
        f"<div class='ew-err-msg'>{safe}</div>"
        "</div></div></body></html>"
    )
    resp = HTMLResponse(body)
    # Fail-closed frame policy on errors: a stale iframe tag on an
    # unexpected domain still can't load us.
    resp.headers["Content-Security-Policy"] = EMBED_CSP_DEFAULT
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Widget payload builders ─────────────────────────────────────────────────


def _pp(p: Optional[float]) -> str:
    """Probability → ``'61%'`` style. Returns ``'—'`` for None."""
    if p is None:
        return "—"
    return f"{int(round(p * 100))}%"


def _edge_confidence_label(ev: Optional[float], avg_cred: Optional[float]) -> str:
    if ev is None:
        return ""
    level = "low confidence"
    if avg_cred is not None:
        if avg_cred >= 0.75:
            level = "high confidence"
        elif avg_cred >= 0.60:
            level = "medium confidence"
    sign = "+" if ev >= 0 else ""
    return f"{sign}{int(round(ev * 100))}pp ({level})"


def _relative_time(ts: Optional[int]) -> str:
    if not ts:
        return ""
    delta = int(time.time()) - int(ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400} days ago"


def _build_source_credibility_payload(handle: str) -> dict:
    cred = None
    if hasattr(db, "get_source_credibility"):
        try:
            cred = db.get_source_credibility(handle)
        except Exception as e:
            log.debug("embed: get_source_credibility(%s) failed: %s", handle, e)
    last_pred = None
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT content, extracted_at FROM predictions "
                "WHERE source_handle = ? ORDER BY extracted_at DESC LIMIT 1",
                (handle,),
            ).fetchone()
            if row:
                last_pred = {
                    "content": (row["content"] or "")[:140],
                    "extracted_at": row["extracted_at"],
                }
    except Exception as e:
        log.debug("embed: last-prediction lookup failed for %s: %s", handle, e)
    return {
        "handle": handle,
        "credibility": cred["global_credibility"] if cred else None,
        "accuracy_unlocked": bool(cred["accuracy_unlocked"]) if cred else False,
        "last_prediction": last_pred,
    }


def _build_market_probability_payload(target: str) -> dict:
    slug = target.split(":", 1)[-1] if ":" in target else target
    market_id_candidates = [target] if ":" in target else [f"poly:{target}", target]
    row = None
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT * FROM market_snapshots WHERE market_slug = ? "
                "ORDER BY snapshotted_at DESC LIMIT 1",
                (slug,),
            ).fetchone()
    except Exception as e:
        log.debug("embed: market lookup failed for %s: %s", slug, e)
    if not row:
        return {"missing": True, "slug": slug}
    market_price = float(row["yes_price"] or 0.0)
    narve_prob = None
    avg_cred = None
    try:
        preds: list = []
        with db.conn() as c:
            for mid in market_id_candidates:
                preds = c.execute(
                    "SELECT p.*, sc.global_credibility FROM predictions p "
                    "LEFT JOIN source_credibility sc "
                    "ON sc.source_handle = p.source_handle "
                    "WHERE p.market_id = ?",
                    (mid,),
                ).fetchall()
                if preds:
                    break
        if preds and hasattr(db, "calculate_betyc_probability"):
            calc = db.calculate_betyc_probability(list(preds))
            narve_prob = calc.get("betyc_yes_probability")
        if preds:
            creds = [float(p["global_credibility"] or 0.5) for p in preds]
            if creds:
                avg_cred = sum(creds) / len(creds)
    except Exception as e:
        log.debug("embed: narve_probability calc failed for %s: %s", slug, e)
    edge = (narve_prob - market_price) if narve_prob is not None else None
    return {
        "missing": False,
        "slug": slug,
        "question": row["market_question"],
        "market_price": market_price,
        "narve_probability": narve_prob,
        "edge": edge,
        "edge_label": _edge_confidence_label(edge, avg_cred),
    }


def _build_best_bets_payload() -> dict:
    """Top-3 highest-EV markets for the public embed widget.

    Caching: 120s TTL via sync `ttl_cache`. The factory runs the work
    below; it's keyed `embed:best_bets:v1` (no per-request params — this
    payload is identical for every embed viewer) so all third-party page
    views share a single cache slot.

    DB shape: previously this issued 1 + N queries (60 markets → 61
    queries against `predictions`). Now it's two: one to fetch the
    snapshot list, one `IN (...)` query that pulls every prediction
    across all candidate markets and groups them in-process by market_id.
    """
    def _compute() -> dict:
        picks: list[dict] = []
        try:
            with db.conn() as c:
                rows = c.execute(
                    "SELECT market_slug, market_question, yes_price "
                    "FROM market_snapshots "
                    "ORDER BY snapshotted_at DESC LIMIT 60"
                ).fetchall()
            seen: set[str] = set()
            deduped = []
            for r in rows:
                if r["market_slug"] in seen:
                    continue
                seen.add(r["market_slug"])
                deduped.append(r)
            if not deduped:
                return {"picks": []}

            # Single batched query: collect every market_id we care about
            # and fetch predictions + credibility in one round-trip, then
            # group by market_id in Python.
            market_ids = [f"poly:{r['market_slug']}" for r in deduped]
            preds_by_market: dict[str, list] = {}
            try:
                placeholders = ",".join("?" * len(market_ids))
                with db.conn() as c:
                    all_preds = c.execute(
                        "SELECT p.*, sc.global_credibility FROM predictions p "
                        "LEFT JOIN source_credibility sc "
                        "ON sc.source_handle = p.source_handle "
                        f"WHERE p.market_id IN ({placeholders})",
                        market_ids,
                    ).fetchall()
                for p in all_preds:
                    preds_by_market.setdefault(p["market_id"], []).append(p)
            except Exception as e:
                log.debug("embed: best_bets batched preds query failed: %s", e)
                return {"picks": []}

            if not hasattr(db, "calculate_betyc_probability"):
                return {"picks": []}

            for r in deduped:
                try:
                    preds = preds_by_market.get(f"poly:{r['market_slug']}", [])
                    if not preds:
                        continue
                    calc = db.calculate_betyc_probability(list(preds))
                    narve_prob = calc.get("betyc_yes_probability")
                    if narve_prob is None:
                        continue
                    market_price = float(r["yes_price"] or 0.0)
                    ev = narve_prob - market_price
                    creds = [float(p["global_credibility"] or 0.5) for p in preds]
                    avg_cred = sum(creds) / len(creds) if creds else 0.5
                    picks.append({
                        "slug": r["market_slug"],
                        "question": r["market_question"],
                        "ev": ev,
                        "credibility": avg_cred,
                    })
                except Exception:
                    continue
        except Exception as e:
            log.debug("embed: best_bets scan failed: %s", e)
        picks.sort(key=lambda p: abs(p["ev"]), reverse=True)
        return {"picks": picks[:3]}

    try:
        from cache import ttl_cache
        return ttl_cache.get_or_compute(
            "embed:best_bets:v1",
            _compute,
            ttl_seconds=120,
        )
    except Exception as e:
        # Cache import/runtime failure must never break the endpoint.
        log.debug("embed: best_bets cache bypass: %s", e)
        return _compute()


# ── Widget body HTML ────────────────────────────────────────────────────────


def _render_source_credibility_body(p: dict) -> str:
    handle = html.escape(p.get("handle") or "")
    cred = p.get("credibility")
    unlocked = p.get("accuracy_unlocked")
    last = p.get("last_prediction") or {}
    if cred is None:
        score = "—"
        score_note = "Not enough data"
    elif not unlocked:
        score = f"{cred:.2f}"
        score_note = "Provisional score"
    else:
        score = f"{cred:.3f}"
        score_note = "Credibility score"
    if last.get("content"):
        rel = _relative_time(last.get("extracted_at"))
        rel_html = f" · {html.escape(rel)}" if rel else ""
        last_html = (
            "<hr class='ew-rule'>"
            f"<div class='ew-subtitle'>Last prediction{rel_html}</div>"
            f"<div class='ew-quote'>&ldquo;{html.escape(last['content'])}&rdquo;</div>"
        )
    else:
        last_html = (
            "<hr class='ew-rule'>"
            "<div class='ew-empty'>No recent predictions on file.</div>"
        )
    return (
        f"<div class='ew-handle'>@{handle}</div>"
        f"<div class='ew-score'>{html.escape(score)}</div>"
        f"<div class='ew-label'>{html.escape(score_note)}</div>"
        f"{last_html}"
    )


def _render_market_probability_body(p: dict) -> str:
    if p.get("missing"):
        return "<div class='ew-empty'>Market not found on narve.ai.</div>"
    q = html.escape(p.get("question") or "")
    market_pp = _pp(p.get("market_price"))
    narve_pp = _pp(p.get("narve_probability"))
    edge_label = p.get("edge_label") or ""
    edge_html = (
        f"<div class='ew-edge'>Edge: {html.escape(edge_label)}</div>"
        if edge_label else ""
    )
    return (
        f"<div class='ew-question'>&ldquo;{q}&rdquo;</div>"
        "<dl class='ew-dl'>"
        f"<div><dt>Market</dt><dd>{market_pp} YES</dd></div>"
        f"<div><dt>narve.ai</dt><dd>{narve_pp} YES</dd></div>"
        "</dl>"
        f"{edge_html}"
    )


def _render_best_bets_body(p: dict) -> str:
    picks = p.get("picks") or []
    if not picks:
        return (
            "<div class='ew-eyebrow'>narve.ai · Top Signals</div>"
            "<div class='ew-empty'>No active picks right now.</div>"
        )
    lis = []
    for pk in picks:
        ev = pk.get("ev") or 0.0
        ev_str = f"{'+' if ev >= 0 else ''}{ev:.3f}"
        cred = pk.get("credibility") or 0.5
        lis.append(
            "<li>"
            f"<span class='ew-pick-q'>&ldquo;{html.escape(pk.get('question') or '')}&rdquo;</span>"
            f"<div class='ew-pick-meta'>EV {ev_str} · {cred:.2f} cred</div>"
            "</li>"
        )
    return (
        "<div class='ew-eyebrow'>narve.ai · Top Signals</div>"
        "<ol class='ew-picks'>" + "".join(lis) + "</ol>"
    )


def _render_embed_widget(widget, payload: dict) -> HTMLResponse:
    wt = widget["widget_type"]
    theme = widget["theme"]
    theme_attr = "" if theme == "auto" else f' data-theme="{html.escape(theme)}"'
    powered = (
        f"{_EMBED_APP_URL}/?utm_source=embed&utm_content="
        f"{html.escape(widget['widget_id'])}"
    )
    if wt == "source_credibility":
        body = _render_source_credibility_body(payload)
    elif wt == "market_probability":
        body = _render_market_probability_body(payload)
    elif wt == "best_bets":
        body = _render_best_bets_body(payload)
    else:
        body = "<div class='ew-empty'>Widget type not recognised.</div>"
    html_out = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>narve.ai widget</title>"
        "<style>" + _embed_css() + "</style></head>"
        f"<body{theme_attr} class='ew-body'>"
        f"<div class='ew-root'>{body}</div>"
        f"<a class='ew-powered' href='{html.escape(powered)}' "
        f"target='_blank' rel='noopener'>"
        f"Powered by <strong>narve.ai</strong></a>"
        "</body></html>"
    )
    return HTMLResponse(html_out)


def _embed_payload_for(widget) -> dict:
    wt = widget["widget_type"]
    if wt == "source_credibility":
        return _build_source_credibility_payload(widget["target"])
    if wt == "market_probability":
        return _build_market_probability_payload(widget["target"])
    if wt == "best_bets":
        return _build_best_bets_payload()
    return {"error": f"Unknown widget type: {wt}"}


# ── Routes ──────────────────────────────────────────────────────────────────


@app.get("/settings/embeds", response_class=HTMLResponse)
async def settings_embeds_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/settings/embeds")
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    username = user["username"] or (user["email"] or "").split("@")[0]
    role_badge = _role_badge(user)
    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""
    _sidebar = render_sidebar(
        request,
        active="settings",
        username=username,
        raw_admin_link=admin_link,
        raw_nav_role=role_badge,
    )
    return render_page(
        "settings_embeds",
        request=request,
        username=username,
        raw_nav_role=role_badge,
        raw_admin_link=admin_link,
        raw_role_badge=role_badge,
        _is_admin=user.get("is_admin"),
        raw_sidebar=_sidebar,
    )


@app.get("/api/embeds")
async def api_list_embeds(request: Request):
    user = _require_authenticated(request)
    widgets = db.list_user_embed_widgets(user["user_id"])
    return JSONResponse({
        "widgets": [_widget_to_api_dict(w, include_token=True) for w in widgets],
        "limit": db.MAX_EMBED_WIDGETS_PER_USER,
        "active_count": db.count_user_active_embed_widgets(user["user_id"]),
    })


@app.post("/api/embeds")
async def api_create_embed(request: Request):
    user = _require_paid_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    widget_type = (body.get("widget_type") or "").strip()
    target = (body.get("target") or "").strip()
    domain = (body.get("domain") or "").strip().lower()
    theme = (body.get("theme") or "auto").strip().lower()

    if widget_type not in db.EMBED_WIDGET_TYPES:
        raise HTTPException(status_code=400, detail="Invalid widget_type")
    if theme not in db.EMBED_WIDGET_THEMES:
        raise HTTPException(status_code=400, detail="Invalid theme")
    if domain.startswith("http://") or domain.startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail="domain must be a bare hostname (no scheme)",
        )
    if not _DOMAIN_RE.match(domain):
        raise HTTPException(
            status_code=400,
            detail="Invalid domain (expected a hostname like example.com)",
        )

    if widget_type == "best_bets":
        target = "top"
    else:
        if not target:
            raise HTTPException(status_code=400, detail="target is required")
        if widget_type == "source_credibility":
            target = target.lstrip("@").lower()

    row = db.create_embed_widget(user["user_id"], widget_type, target, domain, theme)
    if row is None:
        raise HTTPException(
            status_code=403,
            detail=f"Widget limit reached ({db.MAX_EMBED_WIDGETS_PER_USER} per user)",
        )
    log.info(
        "embed widget created: user=%s widget_id=%s type=%s domain=%s",
        user["user_id"], row["widget_id"], widget_type, domain,
    )
    return JSONResponse(
        {"widget": _widget_to_api_dict(row, include_token=True)},
        status_code=201,
    )


@app.delete("/api/embeds/{widget_id}")
async def api_deactivate_embed(request: Request, widget_id: str):
    user = _require_authenticated(request)
    ok = db.deactivate_embed_widget(user["user_id"], widget_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Widget not found")
    return JSONResponse({"deactivated": True, "widget_id": widget_id})


@app.post("/api/embeds/{widget_id}/rotate-token")
async def api_rotate_embed_token(request: Request, widget_id: str):
    user = _require_paid_user(request)
    row = db.rotate_embed_widget_token(user["user_id"], widget_id)
    if not row:
        raise HTTPException(
            status_code=404, detail="Widget not found or already deactivated",
        )
    return JSONResponse({"widget": _widget_to_api_dict(row, include_token=True)})


@app.get("/embed/{widget_id}", response_class=HTMLResponse)
async def serve_embed(request: Request, widget_id: str, token: str = ""):
    widget = db.get_embed_widget_by_widget_id(widget_id)
    if not widget:
        return _render_embed_error("This widget is no longer active")
    if not widget["is_active"]:
        return _render_embed_error("This widget has been deactivated")
    if not embed_tokens.verify(widget["widget_id"], widget["token_salt"], token):
        # Covers both bad-HMAC and expired tokens (H16).
        return _render_embed_error("Invalid or expired token")

    # Domain enforcement via Referer.
    #
    # SECURITY (L16): when the widget has a specific allowlist domain
    # we now REQUIRE a matching Referer. Previously a missing Referer
    # was tolerated outright, which let a token leaked to e.g. a
    # phishing page load the widget as long as the browser stripped
    # Referer — a trivial bypass via `<meta name="referrer"
    # content="no-referrer">`. The CSP frame-ancestors header further
    # down is still the browser-enforced defence-in-depth.
    #
    # Widgets with NO configured domain (open widgets) still tolerate
    # missing Referer since there is nothing to match against.
    widget_domain = (widget["domain"] or "").strip().lower()
    referer = request.headers.get("referer", "")
    if widget_domain:
        if not referer:
            return _render_embed_error(
                f"This widget can only be embedded on {widget_domain}"
            )
        try:
            host = urlparse(referer).netloc.lower().split(":")[0]
        except Exception:
            host = ""
        if not host or host != widget_domain:
            return _render_embed_error(
                f"This widget can only be embedded on {widget_domain}"
            )
    elif referer:
        # No domain configured — still record nothing, but fall through.
        pass

    # Subscription sanity: if the owner's sub has lapsed, bulk-deactivate
    # every widget they own. Prevents stale tokens from leaking data
    # after cancellation.
    if not db.has_any_active_subscription(widget["user_id"]):
        db.deactivate_all_user_embed_widgets(widget["user_id"])
        return _render_embed_error("Subscription required")

    payload = _embed_payload_for(widget)
    html_resp = _render_embed_widget(widget, payload)

    # Fire-and-forget impression bump via the job queue; never block
    # render on queue availability. Falls back to an inline update so
    # the counter still moves during local-dev where the queue might
    # not have started.
    try:
        from jobs import enqueue_job
        await enqueue_job("increment_embed_impression", widget_id=widget_id)
    except Exception as e:
        log.debug("embed impression enqueue failed: %s", e)
        try:
            db.increment_embed_widget_impression(widget_id)
        except Exception:
            pass

    # Per-widget frame-ancestors: only the registered domain (any scheme,
    # to cover http-served dev partners) may iframe this widget. Setting
    # CSP explicitly prevents the middleware from installing the strict
    # site default.
    html_resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "base-uri 'self'; "
        f"frame-ancestors https://{widget['domain']} http://{widget['domain']}"
    )
    # Short max-age so a sub lapse or deactivation propagates within a
    # minute across partner CDNs, and the impression counter stays
    # roughly accurate.
    html_resp.headers["Cache-Control"] = "public, max-age=60"
    return html_resp
