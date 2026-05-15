"""Session-auth settings pages for personal API key management + admin
oversight of every key across the platform.

These routes are for the user's own browser-managed key list — they are
NOT the public API. The public API lives at /api/public/v1/*.

Routes:
  GET  /settings/api-keys              — list page
  POST /settings/api-keys              — create new key (tier-quota gated)
  POST /settings/api-keys/{id}/revoke  — revoke
  GET  /admin/api-keys                 — admin oversight (all keys, all users)
  POST /admin/api-keys/{id}/revoke     — admin force-revoke any key

Per-tier quotas enforced server-side (never trust the client):
  free / trader → 1 key, 1k req/hr
  pro           → 5 keys, 10k req/hr each
  enterprise    → unlimited, custom rate_limit_hour

Keys are minted via ``queries.api_keys.create_api_key`` which returns a
``nv_emb_<32-hex>`` key. The freshly-minted raw key is rendered ONCE on
the response page; only the SHA-256 hash is stored, so it can never be
read back.
"""

from __future__ import annotations

import html
import logging
import sys

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db
from queries import api_keys as q_api_keys


log = logging.getLogger("gateway.api_keys_routes")


# Per-tier caps — single source of truth. Handlers below consult this
# when deciding whether to allow a new key.
_TIER_QUOTAS = {
    "none":       {"max_keys": 0, "rate_limit_hour": 0,      "default_scopes": "read"},
    "free":       {"max_keys": 1, "rate_limit_hour": 1_000,  "default_scopes": "read"},
    "trader":     {"max_keys": 1, "rate_limit_hour": 1_000,  "default_scopes": "read"},
    "pro":        {"max_keys": 5, "rate_limit_hour": 10_000, "default_scopes": "read"},
    "enterprise": {"max_keys": 10, "rate_limit_hour": 100_000, "default_scopes": "read,write"},
}


# ── Deferred lookups into server.py (admin_routes.py pattern) ──────────


def _srv():
    return sys.modules.get("server") or sys.modules["__main__"]


def _current_user(request):
    return _srv().current_user(request)


def _require_admin_user(request, *, page: bool = False):
    return _srv()._require_admin_user(request, page=page)


def _render(name, request, **ctx):
    return _srv().render_page(name, request=request, **ctx)


def _role_badge(user):
    return _srv()._role_badge(user) if hasattr(_srv(), "_role_badge") else ""


# ── Helpers ────────────────────────────────────────────────────────────


def _resolve_tier(user_id: int) -> str:
    try:
        t = db.get_user_subscription_tier(user_id) if hasattr(db, "get_user_subscription_tier") else None
    except Exception:
        t = None
    return (t or "free").strip().lower()


def _quota_for(tier: str) -> dict:
    # Admins/enterprise get the enterprise envelope. Anything unrecognised
    # falls back to 'free' so a stray tier string can never accidentally
    # grant a higher quota than intended.
    return _TIER_QUOTAS.get(tier, _TIER_QUOTAS["free"])


def _active_key_count(user_id: int) -> int:
    rows = db.list_api_keys(user_id)
    return sum(1 for r in rows if not r["revoked_at"])


def _origin_badges(raw: str) -> str:
    """Render an `allowed_origins` comma list as monospaced badges."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not parts:
        return '<span class="ak-badge ak-badge-muted">any origin</span>'
    return "".join(
        f'<span class="ak-badge ak-badge-mono">{html.escape(p)}</span>'
        for p in parts
    )


def _scope_badges(raw: str) -> str:
    parts = [p.strip() for p in (raw or "read").split(",") if p.strip()]
    if not parts:
        parts = ["read"]
    return "".join(
        f'<span class="ak-badge">{html.escape(p)}</span>'
        for p in parts
    )


def _fmt_dt(ts) -> str:
    import datetime as _dt
    if not ts:
        return "never"
    return _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _fmt_date(ts) -> str:
    import datetime as _dt
    if not ts:
        return "—"
    return _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc).strftime(
        "%Y-%m-%d"
    )


# ── Routes: user-facing settings ───────────────────────────────────────


async def api_keys_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login?next=/settings/api-keys", status_code=302)

    tier = _resolve_tier(user["user_id"])
    quota = _quota_for(tier)
    rows = db.list_api_keys(user["user_id"])

    row_html: list[str] = []
    for r in rows:
        created = _fmt_date(r["created_at"])
        last_used = _fmt_dt(r["last_used_at"])
        usage = int(r["usage_count"] or 0)
        origins_raw = r["allowed_origins"] or ""
        scope_badges = _scope_badges(r["scopes"] or "read")
        origin_badges = _origin_badges(origins_raw)

        if r["revoked_at"]:
            status = '<span class="ak-badge ak-badge-muted">REVOKED</span>'
            actions = ""
        else:
            status = '<span class="ak-badge ak-badge-ok">Active</span>'
            actions = (
                f'<form method="post" action="/settings/api-keys/{r["id"]}/revoke" '
                f'style="display:inline" '
                f'onsubmit="return confirm(\'Revoke this key? Any service using '
                f'it will immediately stop working.\')">'
                f'<button class="ak-btn ak-btn-danger" type="submit">Revoke</button>'
                f'</form>'
            )
        row_html.append(
            '<div class="ak-row">'
            '<div class="ak-row-main">'
            f'<div class="ak-row-head"><code class="ak-mono">{html.escape(r["key_prefix"])}…</code> '
            f'<strong>{html.escape(r["name"] or "(unnamed)")}</strong> {status}</div>'
            f'<div class="ak-row-meta">'
            f'<span class="ak-meta-k">Scopes</span> {scope_badges} '
            f'<span class="ak-meta-k">Origins</span> {origin_badges}'
            f'</div>'
            f'<div class="ak-row-meta">'
            f'<span class="ak-meta-k">Calls</span> '
            f'<code class="ak-mono">{usage:,}</code> total · '
            f'<span class="ak-meta-k">Limit</span> '
            f'<code class="ak-mono">{int(r["rate_limit_hour"]):,}</code>/hr · '
            f'<span class="ak-meta-k">Created</span> {created} · '
            f'<span class="ak-meta-k">Last used</span> {last_used}'
            f'</div>'
            f'</div>'
            f'<div class="ak-row-actions">{actions}</div>'
            '</div>'
        )
    if not row_html:
        row_html.append(
            '<div class="ak-row"><div class="ak-row-main">'
            '<div class="ak-row-meta">No keys yet. Create one below.</div>'
            '</div></div>'
        )

    active = _active_key_count(user["user_id"])
    at_quota = active >= quota["max_keys"]
    quota_msg = (
        f"Tier <strong>{html.escape(tier)}</strong> — "
        f"{active}/{quota['max_keys']} keys used · "
        f"{quota['rate_limit_hour']:,}/hour per key"
    )

    return _render(
        "settings_api_keys",
        request=request,
        email=user["email"],
        username=user.get("username", user["email"]),
        raw_nav_role=_role_badge(user),
        raw_key_rows="".join(row_html),
        raw_quota_line=quota_msg,
        create_disabled="disabled" if at_quota else "",
        create_disabled_note=(
            "You are at your plan's key limit. Revoke an unused key or upgrade "
            "to add another."
            if at_quota else ""
        ),
        can_request_write=("1" if quota.get("default_scopes", "read") != "read" else "0"),
    )


async def api_keys_create(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Login required")
    form = await request.form()
    name = (form.get("name") or "").strip()[:80] or "untitled"
    want_write = str(form.get("scope_write") or "").strip() == "1"
    origins_raw = (form.get("allowed_origins") or "").strip()

    tier = _resolve_tier(user["user_id"])
    quota = _quota_for(tier)
    if quota["max_keys"] <= 0:
        raise HTTPException(402, "Your plan doesn't include API access. Upgrade to create a key.")
    if _active_key_count(user["user_id"]) >= quota["max_keys"]:
        raise HTTPException(409, "At key limit for this tier. Revoke an existing key first.")

    # Scope: always at least read. Pro / enterprise can also be granted
    # write on request. Anything below pro silently drops the write
    # request — the form checkbox already greys out, this is a
    # defence-in-depth check.
    scopes_list = ["read"]
    if want_write:
        default_scopes = quota.get("default_scopes", "read").split(",")
        if "write" in default_scopes or tier in ("pro", "enterprise"):
            scopes_list.append("write")
    scopes = ",".join(scopes_list)

    try:
        raw_key, _ = q_api_keys.create_api_key(
            user_id=user["user_id"],
            name=name,
            scopes=scopes,
            origins=origins_raw,
            tier="embed",
            rate_limit_hour=quota["rate_limit_hour"],
        )
    except Exception as exc:
        log.exception("create_api_key failed user=%s: %s", user["user_id"], exc)
        raise HTTPException(500, "Could not mint key")

    # Audit (best-effort).
    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=user["user_id"], admin_email=user["email"],
            action="api_key.create",
            target_type="api_key", target_id=raw_key[:12],
            request=request,
            notes=f"scopes={scopes} tier={tier} origins={origins_raw or 'any'}",
        )
    except Exception:
        pass

    # Render a one-time reveal page. NEVER redirect — on redirect the
    # raw key would be lost.
    return _render(
        "settings_api_key_reveal",
        request=request,
        email=user["email"],
        username=user.get("username", user["email"]),
        raw_nav_role=_role_badge(user),
        raw_key=raw_key,
        key_name=name,
        key_prefix=raw_key[:12],
        key_scopes=", ".join(scopes_list),
        key_rate=f"{quota['rate_limit_hour']:,}/hour",
        key_origins=origins_raw or "any origin",
    )


async def api_keys_revoke(request: Request, key_id: int):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Login required")
    ok = q_api_keys.revoke_api_key(key_id, user["user_id"])
    if ok:
        try:
            from security import audit as _audit
            _audit.log_action(
                admin_user_id=user["user_id"], admin_email=user["email"],
                action="api_key.revoke",
                target_type="api_key", target_id=key_id,
                request=request,
            )
        except Exception:
            pass
    return RedirectResponse("/settings/api-keys", status_code=302)


# ── Routes: admin oversight ────────────────────────────────────────────


async def admin_api_keys_page(request: Request):
    """Every API key across every user — admin-only.

    Read-only listing + per-key revoke button. Bypasses the per-user
    quota check because admins are operating outside the tier system.
    """
    admin = _require_admin_user(request, page=True)
    if admin is None:
        # SECURITY: _require_admin_user(page=True) returns None (not a
        # RedirectResponse) for non-admins. Without this check, the
        # hasattr() guard below lets the request flow through and
        # ``list_all_api_keys()`` leaks every tenant's keys.
        return RedirectResponse("/login?next=/admin/api-keys", status_code=302)
    if hasattr(admin, "status_code"):  # belt-and-braces — Response/RedirectResponse paths (e.g. 2FA)
        return admin

    rows = q_api_keys.list_all_api_keys()
    row_html: list[str] = []
    for r in rows:
        created = _fmt_date(r["created_at"])
        last_used = _fmt_dt(r["last_used_at"])
        usage = int((r["usage_count"] if "usage_count" in r.keys() else 0) or 0)
        origins_raw = r["allowed_origins"] if "allowed_origins" in r.keys() else ""
        scope_badges = _scope_badges(r["scopes"] or "read")
        origin_badges = _origin_badges(origins_raw or "")

        owner = r["owner_email"] if "owner_email" in r.keys() else "(unknown)"
        owner_safe = html.escape(owner or "(unknown)")

        if r["revoked_at"]:
            status = '<span class="ak-badge ak-badge-muted">REVOKED</span>'
            actions = ""
        else:
            status = '<span class="ak-badge ak-badge-ok">Active</span>'
            actions = (
                f'<form method="post" action="/admin/api-keys/{r["id"]}/revoke" '
                f'style="display:inline" '
                f'onsubmit="return confirm(\'Force-revoke key for ' + owner_safe + '?\')">'
                f'<button class="ak-btn ak-btn-danger" type="submit">Revoke</button>'
                f'</form>'
            )

        row_html.append(
            '<div class="ak-row">'
            '<div class="ak-row-main">'
            f'<div class="ak-row-head">'
            f'<code class="ak-mono">{html.escape(r["key_prefix"])}…</code> '
            f'<strong>{html.escape(r["name"] or "(unnamed)")}</strong> {status}'
            f'</div>'
            f'<div class="ak-row-meta">'
            f'<span class="ak-meta-k">Owner</span> '
            f'<a href="/admin/users?q={html.escape(owner)}" '
            f'   style="color:inherit">{owner_safe}</a> · '
            f'<span class="ak-meta-k">Scopes</span> {scope_badges} '
            f'<span class="ak-meta-k">Origins</span> {origin_badges}'
            f'</div>'
            f'<div class="ak-row-meta">'
            f'<span class="ak-meta-k">Calls</span> '
            f'<code class="ak-mono">{usage:,}</code> total · '
            f'<span class="ak-meta-k">Created</span> {created} · '
            f'<span class="ak-meta-k">Last used</span> {last_used}'
            f'</div>'
            f'</div>'
            f'<div class="ak-row-actions">{actions}</div>'
            '</div>'
        )
    if not row_html:
        row_html.append(
            '<div class="ak-row"><div class="ak-row-main">'
            '<div class="ak-row-meta">No API keys minted yet.</div>'
            '</div></div>'
        )

    return _render(
        "admin_api_keys",
        request=request,
        raw_nav_role=_role_badge(admin),
        raw_key_rows="".join(row_html),
        total=len(rows),
    )


async def admin_api_keys_revoke(request: Request, key_id: int):
    admin = _require_admin_user(request)
    if admin is None:
        # SECURITY: defence-in-depth. page=False normally raises
        # HTTPException(403); if the contract ever changes, we still
        # must not execute the cross-tenant revoke below.
        return RedirectResponse("/login?next=/admin/api-keys", status_code=302)
    if hasattr(admin, "status_code"):  # belt-and-braces — Response/RedirectResponse paths
        return admin
    ok = q_api_keys.admin_revoke_api_key(int(key_id))
    if ok:
        try:
            from security import audit as _audit
            _audit.log_action(
                admin_user_id=admin["user_id"], admin_email=admin["email"],
                action="api_key.admin_revoke",
                target_type="api_key", target_id=int(key_id),
                request=request,
            )
        except Exception:
            pass
    return RedirectResponse("/admin/api-keys", status_code=302)


# ── Registration ───────────────────────────────────────────────────────


def register(app) -> None:
    app.add_api_route("/settings/api-keys", api_keys_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/settings/api-keys", api_keys_create,
                      methods=["POST"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/settings/api-keys/{key_id}/revoke", api_keys_revoke,
                      methods=["POST"], include_in_schema=False)

    app.add_api_route("/admin/api-keys", admin_api_keys_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/admin/api-keys/{key_id}/revoke", admin_api_keys_revoke,
                      methods=["POST"], include_in_schema=False)
