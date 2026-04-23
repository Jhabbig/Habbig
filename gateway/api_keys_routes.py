"""Session-auth settings pages for personal API key management.

These routes are for the user's own browser-managed key list — they are
NOT the public API. The public API lives at /api/public/v1/*.

Routes:
  GET  /settings/api-keys              — list page
  POST /settings/api-keys              — create new key (tier-quota gated)
  POST /settings/api-keys/{id}/revoke  — revoke

Per-tier quotas enforced server-side (never trust the client):
  free / trader → 1 key, 1k req/hr
  pro           → 5 keys, 10k req/hr each
  enterprise    → unlimited, custom rate_limit_hour

Keys are minted via the existing helpers in api_v1.py (stay with the one
canonical create path — no second implementation). The freshly-minted raw
key is rendered ONCE on the response page; we never read it back.
"""

from __future__ import annotations

import html
import json
import logging
import sys

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db


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


# ── Routes ─────────────────────────────────────────────────────────────


async def api_keys_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login?next=/settings/api-keys", status_code=302)

    tier = _resolve_tier(user["user_id"])
    quota = _quota_for(tier)
    rows = db.list_api_keys(user["user_id"])

    import datetime as _dt
    row_html: list[str] = []
    for r in rows:
        created = _dt.datetime.fromtimestamp(int(r["created_at"]), tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        last_used = (
            _dt.datetime.fromtimestamp(int(r["last_used_at"]), tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if r["last_used_at"] else "never"
        )
        scopes = (r["scopes"] or "read").split(",")
        scope_badges = "".join(
            f'<span class="badge">{html.escape(s.strip())}</span>'
            for s in scopes if s.strip()
        )
        if r["revoked_at"]:
            status = '<span class="badge badge-muted">REVOKED</span>'
            actions = ""
        else:
            status = '<span class="badge badge-ok">Active</span>'
            actions = (
                f'<form method="post" action="/settings/api-keys/{r["id"]}/revoke" '
                f'style="display:inline" onsubmit="return confirm(\'Revoke this key? Any service using it will immediately stop working.\')">'
                f'<button class="btn btn-danger" type="submit">Revoke</button></form>'
            )
        row_html.append(
            '<div class="row">'
            f'<div class="row-main">'
            f'  <div><code>{html.escape(r["key_prefix"])}…</code> '
            f'  <strong>{html.escape(r["name"] or "(unnamed)")}</strong> {status}</div>'
            f'  <div class="row-meta">Scopes: {scope_badges} · '
            f'  Limit: {int(r["rate_limit_hour"])}/hr · '
            f'  Created {created} · Last used {last_used}</div>'
            f'</div>'
            f'<div class="row-actions">{actions}</div>'
            '</div>'
        )
    if not row_html:
        row_html.append(
            '<div class="row"><div class="row-main"><div class="row-meta">'
            'No keys yet. Create one below.</div></div></div>'
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
            "You are at your plan's key limit. Revoke an unused key or upgrade to add another."
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

    tier = _resolve_tier(user["user_id"])
    quota = _quota_for(tier)
    if quota["max_keys"] <= 0:
        raise HTTPException(402, "Your plan doesn't include API access. Upgrade to create a key.")
    if _active_key_count(user["user_id"]) >= quota["max_keys"]:
        raise HTTPException(409, "At key limit for this tier. Revoke an existing key first.")

    # Scope: always at least read. Only enterprise can write without review;
    # pro can REQUEST write and we grant it (consistent with quota defaults).
    scopes = ["read"]
    if want_write:
        default_scopes = quota.get("default_scopes", "read").split(",")
        if "write" in default_scopes or tier in ("pro", "enterprise"):
            scopes.append("write")

    # Mint via the existing canonical helper in api_v1.py.
    try:
        import api_v1
        raw_key, key_id = api_v1.create_api_key(
            user_id=user["user_id"], name=name,
            tier="enterprise" if tier == "enterprise" else "standard",
        )
    except Exception as exc:
        log.exception("create_api_key failed user=%s: %s", user["user_id"], exc)
        raise HTTPException(500, "Could not mint key")

    # Apply scope + quota from our tier table (create_api_key picks a
    # generic default). Single authoritative UPDATE right after insert.
    try:
        with db.conn() as c:
            c.execute(
                "UPDATE api_keys SET scopes = ?, rate_limit_hour = ? WHERE id = ?",
                (",".join(scopes), quota["rate_limit_hour"], key_id),
            )
    except Exception as exc:
        log.warning("api_keys post-insert update failed id=%s: %s", key_id, exc)

    # Audit (best-effort).
    try:
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=user["user_id"], admin_email=user["email"],
            action="api_key.create",
            target_type="api_key", target_id=key_id,
            request=request, notes=f"scopes={','.join(scopes)} tier={tier}",
        )
    except Exception:
        pass

    # Render a one-time reveal page (NOT a redirect — on redirect the raw
    # key would be lost). Still monochrome, still auto-CSRF'd.
    return _render(
        "settings_api_key_reveal",
        request=request,
        email=user["email"],
        username=user.get("username", user["email"]),
        raw_nav_role=_role_badge(user),
        raw_key=raw_key,
        key_name=name,
        key_prefix=raw_key[:12],
        key_scopes=", ".join(scopes),
        key_rate=f"{quota['rate_limit_hour']:,}/hour",
    )


async def api_keys_revoke(request: Request, key_id: int):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Login required")
    ok = db.revoke_api_key(key_id, user["user_id"])
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
