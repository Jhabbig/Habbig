"""Queries for the api_keys table — embed-API key management surface.

These helpers complement (don't replace) the legacy public-API helpers
in db.py and api_v1.py. Two key formats coexist:

  - ``narve_<urlsafe>``  → public developer API (api_v1.create_api_key)
  - ``nv_emb_<32hex>``   → embed-API keys minted via /settings/api-keys
                           (this module)

Both formats land in the same api_keys table because they share auth,
revocation, rate-limit, scope, and usage-tracking machinery — the
prefix is only a UI hint so users can tell their embed keys apart from
older API tokens.

Hash discipline: the raw key is shown ONCE at creation. Only the
SHA-256 hex digest is persisted. validate_api_key() re-hashes the
incoming string and looks it up.

Origin allowlist semantics:
  - allowed_origins is comma-separated bare hostnames ("example.com,
    foo.bar.com"). Empty/None means "open key — no origin check".
  - When set, the caller passes the request's Origin or Referer host
    into validate_api_key(..., origin=...) and we 403 on mismatch.
  - Hostname comparison is case-insensitive; ports and paths are
    stripped at the call site.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

import db


log = logging.getLogger("queries.api_keys")


# Prefix for embed-API keys minted through this module. Distinct from
# the ``narve_`` prefix used by the public developer API so users can
# tell them apart at a glance in their settings list.
EMBED_KEY_PREFIX = "nv_emb_"


# ── Hashing + format ─────────────────────────────────────────────────────


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _mint_raw_key() -> str:
    """Generate ``nv_emb_<32-char-hex>``.

    32 hex chars = 128 bits of entropy, sourced via ``secrets.token_hex``
    so it's CSPRNG-backed. The prefix is recognisable in logs and grep
    output without being long enough to crowd out the entropy.
    """
    return f"{EMBED_KEY_PREFIX}{secrets.token_hex(16)}"


def _normalise_origin(origin: Optional[str]) -> str:
    """Reduce an Origin/Referer string to a bare lowercase hostname.

    Accepts full URLs (``https://example.com/foo``), bare hostnames
    (``example.com``), or empty/None. Returns ``""`` on anything
    unparseable so the caller treats it as "no origin provided".
    """
    if not origin:
        return ""
    s = origin.strip().lower()
    if "://" in s:
        try:
            host = urlparse(s).netloc
        except Exception:
            host = ""
        host = host.split("@")[-1]  # strip userinfo if present
        host = host.split(":", 1)[0]  # strip port
        return host
    # Already a bare hostname — strip stray ports/paths defensively.
    return s.split("/", 1)[0].split(":", 1)[0]


def _parse_origin_list(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for part in str(raw).split(","):
        norm = _normalise_origin(part)
        if norm:
            out.append(norm)
    return out


# ── CRUD ─────────────────────────────────────────────────────────────────


def create_api_key(
    user_id: int,
    name: str,
    scopes: str = "read",
    origins: Optional[str] = None,
    *,
    tier: str = "embed",
    rate_limit_hour: int = 1000,
) -> tuple[str, str]:
    """Create a new embed-API key for ``user_id``.

    Returns ``(raw_key, key_hash)``. The raw key is shown ONCE; only the
    hash is persisted. Callers MUST surface the raw key to the user
    immediately and never read it back.

    ``scopes`` is a comma-separated list. ``origins`` is comma-separated
    hostnames (NULL/empty means no origin restriction). ``tier`` is a
    free-form label used by ops dashboards; ``rate_limit_hour`` is the
    per-hour quota enforced by validate_api_key().
    """
    raw_key = _mint_raw_key()
    key_hash = _hash_key(raw_key)
    prefix = raw_key[:12]
    now = int(time.time())

    name = (name or "").strip()[:80] or "untitled"
    scopes = (scopes or "read").strip() or "read"

    # Normalise origins into a canonical comma-joined hostname list so
    # the column always stores in the same shape regardless of how the
    # caller spelled the input.
    origins_list = _parse_origin_list(origins)
    origins_value = ",".join(origins_list) if origins_list else None

    with db.conn() as c:
        # The base table is migration 014; migration 128 added `scopes`;
        # migration 179 added `allowed_origins` + `usage_count`. We
        # write to all of those columns explicitly so the row is
        # complete regardless of whether the legacy default kicks in.
        c.execute(
            "INSERT INTO api_keys "
            "(key_hash, key_prefix, user_id, name, tier, rate_limit_hour, "
            " scopes, allowed_origins, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key_hash, prefix, int(user_id), name, tier,
             int(rate_limit_hour), scopes, origins_value, now),
        )

    return raw_key, key_hash


def validate_api_key(
    raw_key: str,
    *,
    required_scope: Optional[str] = None,
    origin: Optional[str] = None,
) -> Optional[dict]:
    """SHA-256 hash + lookup + revocation/scope/origin checks.

    Returns the key row as a dict on success, or ``None`` if the key is
    missing, revoked, fails the scope check, or fails the origin
    allowlist. Callers that need to differentiate (e.g. 401 vs 403)
    should call ``record_usage`` separately and inspect the result.

    Side effects: bumps ``usage_count`` and stamps ``last_used_at`` on
    every successful validation. (For per-hour quota enforcement use
    ``db.bump_api_usage`` separately — that's a different counter.)
    """
    if not raw_key:
        return None
    raw_key = raw_key.strip()
    if not raw_key:
        return None

    key_hash = _hash_key(raw_key)
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
    if row is None:
        return None
    if row["revoked_at"]:
        return None

    # Scope check. The stored scopes column is comma-separated; we
    # treat ``read`` as implicit baseline (same convention as the public
    # API auth module).
    if required_scope:
        scopes = {
            s.strip() for s in (row["scopes"] or "read").split(",") if s.strip()
        }
        scopes.add("read")
        if required_scope not in scopes:
            return None

    # Origin check. Only enforced when allowed_origins is populated.
    allowed_raw = ""
    try:
        allowed_raw = row["allowed_origins"] or ""
    except (KeyError, IndexError):
        # Migration 179 may not yet have run; treat as open key.
        allowed_raw = ""
    allowed = _parse_origin_list(allowed_raw)
    if allowed:
        caller = _normalise_origin(origin)
        if not caller or caller not in allowed:
            return None

    record_usage(int(row["id"]))

    # Return a plain dict so callers can pickle / cache / serialise.
    out = {k: row[k] for k in row.keys()}
    out["scopes_list"] = sorted({
        s.strip() for s in (out.get("scopes") or "read").split(",") if s.strip()
    } | {"read"})
    out["allowed_origins_list"] = allowed
    return out


def list_api_keys(user_id: int) -> list:
    """All keys for *user_id*, newest first — revoked or not.

    Re-exports the same shape as the legacy ``db.list_api_keys`` but
    also pulls in the new ``allowed_origins`` + ``usage_count``
    columns. Returns sqlite3.Row objects.
    """
    with db.conn() as c:
        return c.execute(
            "SELECT id, user_id, key_prefix, name, tier, scopes, "
            "       allowed_origins, rate_limit_hour, usage_count, "
            "       created_at, last_used_at, revoked_at "
            "FROM api_keys WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (int(user_id),),
        ).fetchall()


def revoke_api_key(key_id: int, user_id: int) -> bool:
    """Idempotent revoke scoped to the owner.

    Returns True iff a row was actually marked revoked. Mirrors the
    legacy helper so callers can swap modules without changing the
    contract. Admin-scoped revocation (any user) is a separate helper —
    see ``admin_revoke_api_key``.
    """
    with db.conn() as c:
        cur = c.execute(
            "UPDATE api_keys SET revoked_at = ? "
            "WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (int(time.time()), int(key_id), int(user_id)),
        )
        return cur.rowcount > 0


def admin_revoke_api_key(key_id: int) -> bool:
    """Admin revoke — no user_id scope. Caller MUST already have
    confirmed admin status before calling this.
    """
    with db.conn() as c:
        cur = c.execute(
            "UPDATE api_keys SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (int(time.time()), int(key_id)),
        )
        return cur.rowcount > 0


def record_usage(key_id: int) -> None:
    """Bump usage_count + last_used_at. Best-effort — never raises.

    Called automatically from validate_api_key(). Exposed publicly so
    callers that take alternative validation paths (e.g. the legacy
    Bearer middleware) can keep the counter in sync.
    """
    now = int(time.time())
    try:
        with db.conn() as c:
            c.execute(
                "UPDATE api_keys SET usage_count = COALESCE(usage_count, 0) + 1, "
                "                    last_used_at = ? "
                "WHERE id = ?",
                (now, int(key_id)),
            )
    except Exception as exc:  # pragma: no cover — cosmetic counter
        log.debug("record_usage failed key_id=%s: %s", key_id, exc)


def list_all_api_keys() -> list:
    """Admin oversight — every key across every user, newest first.

    Joined to users(id) so the admin dashboard can show "owner email"
    without doing N lookups in the template.
    """
    with db.conn() as c:
        return c.execute(
            "SELECT k.id, k.user_id, k.key_prefix, k.name, k.tier, k.scopes, "
            "       k.allowed_origins, k.rate_limit_hour, k.usage_count, "
            "       k.created_at, k.last_used_at, k.revoked_at, "
            "       u.email AS owner_email "
            "FROM api_keys k "
            "LEFT JOIN users u ON u.id = k.user_id "
            "ORDER BY k.created_at DESC"
        ).fetchall()
