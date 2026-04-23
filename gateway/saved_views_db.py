"""DB layer for ``saved_views`` — CRUD, default-toggle, pin-toggle, share.

Keeps the heavy logic out of saved_views_routes.py. Each function
returns plain dicts (not ``sqlite3.Row``) so routes can json-serialise
without an extra conversion.

The share-token scheme is deliberately simple: HMAC-SHA256(secret, "v:{id}")
truncated to 16 bytes and base64url-encoded. No timestamp — saved views
are revocable by toggling ``is_active``, not by token expiry. A token
stops working the moment the view is deleted or the owner's subscription
lapses (enforced at the route layer).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

import db as _db

MAX_VIEWS_PER_USER_PER_SCOPE = 20


# ── Share-token signing ──────────────────────────────────────────────────────


def _signing_secret() -> bytes:
    """Pulled from GATEWAY_SIGNING_SECRET or GATEWAY_SSO_SECRET (same semantics
    as embed_tokens). Tests set EMBED_SIGNING_SECRET — we accept that too so
    a single env value covers every HMAC site.
    """
    key = (
        os.environ.get("GATEWAY_SIGNING_SECRET")
        or os.environ.get("GATEWAY_SSO_SECRET")
        or os.environ.get("EMBED_SIGNING_SECRET")
    )
    if key:
        return key.encode("utf-8")
    # Dev fallback: random per-process. Tokens won't survive a restart in
    # dev, which is fine — prod/staging always set the env.
    global _DEV_FALLBACK
    try:
        return _DEV_FALLBACK
    except NameError:
        _DEV_FALLBACK = secrets.token_bytes(32)
        return _DEV_FALLBACK


def sign_view_token(view_id: int) -> str:
    """Stateless, revocable-only-by-deletion token for /v/{token} shares."""
    payload = f"v:{view_id}".encode("utf-8")
    sig = hmac.new(_signing_secret(), payload, hashlib.sha256).digest()[:16]
    token_bytes = str(view_id).encode("utf-8") + b":" + base64.urlsafe_b64encode(sig).rstrip(b"=")
    return base64.urlsafe_b64encode(token_bytes).rstrip(b"=").decode("ascii")


def verify_view_token(token: str) -> Optional[int]:
    """Decode a share token. Returns the view_id iff signature matches."""
    if not token or not isinstance(token, str):
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        parts = raw.split(b":", 1)
        if len(parts) != 2:
            return None
        view_id_bytes, sig_b64 = parts
        view_id = int(view_id_bytes.decode("utf-8"))
        expected = hmac.new(_signing_secret(), f"v:{view_id}".encode("utf-8"), hashlib.sha256).digest()[:16]
        given = base64.urlsafe_b64decode(sig_b64 + b"=" * (-len(sig_b64) % 4))
        if not hmac.compare_digest(expected, given):
            return None
        return view_id
    except (ValueError, TypeError):
        return None


# ── Row → dict ───────────────────────────────────────────────────────────────


def _row_to_dict(row) -> dict:
    filter_json = row["filter_json"] or "{}"
    try:
        filters = json.loads(filter_json)
    except (ValueError, TypeError):
        filters = {}
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "scope": row["scope"],
        "name": row["name"],
        "filters": filters,
        "is_default": bool(row["is_default"]),
        "is_pinned": bool(row["is_pinned"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "share_token": sign_view_token(row["id"]),
    }


# ── CRUD ─────────────────────────────────────────────────────────────────────


def count_user_views(user_id: int, scope: str) -> int:
    with _db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM saved_views WHERE user_id = ? AND scope = ?",
            (user_id, scope),
        ).fetchone()
    return row["n"] if row else 0


def create_view(
    user_id: int,
    scope: str,
    name: str,
    filters: dict,
    *,
    is_default: bool = False,
    is_pinned: bool = False,
) -> Optional[dict]:
    """Create a saved view. Returns the row or ``None`` if the user is at
    the per-scope limit."""
    name = (name or "").strip()[:80] or "Untitled view"
    filter_json = json.dumps(filters or {}, sort_keys=True, separators=(",", ":"))
    now = int(time.time())
    with _db.conn() as c:
        existing = c.execute(
            "SELECT COUNT(*) AS n FROM saved_views WHERE user_id = ? AND scope = ?",
            (user_id, scope),
        ).fetchone()
        if existing and existing["n"] >= MAX_VIEWS_PER_USER_PER_SCOPE:
            return None
        if is_default:
            c.execute(
                "UPDATE saved_views SET is_default = 0 WHERE user_id = ? AND scope = ?",
                (user_id, scope),
            )
        cur = c.execute(
            "INSERT INTO saved_views "
            "(user_id, scope, name, filter_json, is_default, is_pinned, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, scope, name, filter_json,
             1 if is_default else 0, 1 if is_pinned else 0, now, now),
        )
        row = c.execute(
            "SELECT * FROM saved_views WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def list_user_views(user_id: int, scope: Optional[str] = None) -> list[dict]:
    with _db.conn() as c:
        if scope:
            rows = c.execute(
                "SELECT * FROM saved_views WHERE user_id = ? AND scope = ? "
                "ORDER BY is_pinned DESC, is_default DESC, created_at DESC",
                (user_id, scope),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM saved_views WHERE user_id = ? "
                "ORDER BY scope ASC, is_pinned DESC, is_default DESC, created_at DESC",
                (user_id,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_pinned(user_id: int) -> list[dict]:
    """For sidebar injection — only pinned views, across scopes."""
    with _db.conn() as c:
        rows = c.execute(
            "SELECT * FROM saved_views WHERE user_id = ? AND is_pinned = 1 "
            "ORDER BY scope ASC, created_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_default(user_id: int, scope: str) -> Optional[dict]:
    with _db.conn() as c:
        row = c.execute(
            "SELECT * FROM saved_views WHERE user_id = ? AND scope = ? AND is_default = 1 "
            "LIMIT 1",
            (user_id, scope),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_view(view_id: int) -> Optional[dict]:
    """Scope-less lookup by id — used by /v/{token} share resolution."""
    with _db.conn() as c:
        row = c.execute(
            "SELECT * FROM saved_views WHERE id = ?", (view_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_user_view(user_id: int, view_id: int) -> Optional[dict]:
    """Scoped lookup — returns only if ``user_id`` owns the row. Used by
    every mutation so ACL is enforced in the DB layer, not the handler."""
    with _db.conn() as c:
        row = c.execute(
            "SELECT * FROM saved_views WHERE user_id = ? AND id = ?",
            (user_id, view_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def update_view(
    user_id: int,
    view_id: int,
    *,
    name: Optional[str] = None,
    filters: Optional[dict] = None,
    is_default: Optional[bool] = None,
    is_pinned: Optional[bool] = None,
) -> Optional[dict]:
    """Partial update. Returns the updated row or ``None`` if not owned."""
    with _db.conn() as c:
        existing = c.execute(
            "SELECT * FROM saved_views WHERE user_id = ? AND id = ?",
            (user_id, view_id),
        ).fetchone()
        if not existing:
            return None
        if is_default is True:
            # Clear other defaults in the same scope first.
            c.execute(
                "UPDATE saved_views SET is_default = 0 "
                "WHERE user_id = ? AND scope = ? AND id != ?",
                (user_id, existing["scope"], view_id),
            )
        sets = []
        params: list = []
        if name is not None:
            sets.append("name = ?")
            params.append((name or "").strip()[:80] or "Untitled view")
        if filters is not None:
            sets.append("filter_json = ?")
            params.append(json.dumps(filters or {}, sort_keys=True, separators=(",", ":")))
        if is_default is not None:
            sets.append("is_default = ?")
            params.append(1 if is_default else 0)
        if is_pinned is not None:
            sets.append("is_pinned = ?")
            params.append(1 if is_pinned else 0)
        if not sets:
            return _row_to_dict(existing)
        sets.append("updated_at = ?")
        params.append(int(time.time()))
        params.extend([user_id, view_id])
        c.execute(
            "UPDATE saved_views SET " + ", ".join(sets) + " "
            "WHERE user_id = ? AND id = ?",
            params,
        )
        row = c.execute(
            "SELECT * FROM saved_views WHERE id = ?", (view_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def delete_view(user_id: int, view_id: int) -> bool:
    with _db.conn() as c:
        cur = c.execute(
            "DELETE FROM saved_views WHERE user_id = ? AND id = ?",
            (user_id, view_id),
        )
    return cur.rowcount > 0


def clone_view(recipient_user_id: int, source_view_id: int, *, name: Optional[str] = None) -> Optional[dict]:
    """Duplicate someone else's view into the recipient's saved_views.

    The clone is always non-default, non-pinned — recipient has to flip
    those themselves. Respects the recipient's per-scope limit.
    """
    source = get_view(source_view_id)
    if not source:
        return None
    new_name = name or f"Clone of {source['name']}"
    return create_view(
        recipient_user_id,
        source["scope"],
        new_name,
        source["filters"],
        is_default=False,
        is_pinned=False,
    )
