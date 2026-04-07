"""Supabase layer for the gateway — users, sessions, subscriptions.

Replaces the previous SQLite implementation. Uses Supabase for Postgres
storage and keeps the same function signatures so server.py requires
minimal changes.

Required environment variables:
    SUPABASE_URL            - Your Supabase project URL
    SUPABASE_SERVICE_KEY    - Service role key (server-side, bypasses RLS)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

from supabase import create_client, Client

log = logging.getLogger("gateway.db")

# ── Supabase client ─────────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

_client: Optional[Client] = None


def _get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set. "
                "Create a project at https://supabase.com and set these env vars."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


def init_db() -> None:
    """Verify Supabase connection is working. Called on startup."""
    client = _get_client()
    # Quick health check — fetch zero rows from profiles
    try:
        client.table("profiles").select("id").limit(0).execute()
        log.info("Supabase connection OK")
    except Exception as e:
        log.error("Supabase connection failed: %s", e)
        raise


# ── Helper to convert Supabase row to dict ──────────────────────────────────

class Row(dict):
    """Dict subclass that supports both dict['key'] and dict.key access,
    mimicking sqlite3.Row interface for backward compatibility."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def keys(self):
        return super().keys()


def _row(data: Optional[dict]) -> Optional[Row]:
    if data is None:
        return None
    return Row(data)


def _rows(data: list[dict]) -> list[Row]:
    return [Row(d) for d in data]


# ── Password hashing ────────────────────────────────────────────────────────
# Using PBKDF2-HMAC-SHA256 (stdlib, no external deps). 200k iterations.


def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return dk.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """Legacy: verify password against a stored PBKDF2 hash (for migration only)."""
    candidate, _ = _hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def verify_user_password(email: str, password: str) -> bool:
    """Verify a user's password via Supabase Auth sign-in attempt.

    Uses a throwaway client to avoid corrupting the main service-role
    client's auth state (sign_in_with_password swaps the postgrest
    Authorization header from service-role to user JWT).
    """
    try:
        temp_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        result = temp_client.auth.sign_in_with_password({"email": email, "password": password})
        return result.user is not None
    except Exception:
        return False


def link_invite_token_to_user(user_id: str, token_str: str) -> None:
    """Set the invite_token_id on a user's profile from a token string."""
    client = _get_client()
    token_row = client.table("invite_tokens").select("id").eq("token", token_str).limit(1).execute()
    if token_row.data:
        client.table("profiles").update({
            "invite_token_id": token_row.data[0]["id"]
        }).eq("id", user_id).execute()


# ── User operations ─────────────────────────────────────────────────────────


def create_user(email: str, password: str, username: str = "", is_admin: bool = False, admin_level: int = 0) -> str:
    """Create a user via Supabase Auth + profiles table. Returns the user UUID string."""
    email = email.lower().strip()
    username = username.strip()
    if not username:
        username = email.split("@")[0]
    level = admin_level if admin_level else (1 if is_admin else 0)

    client = _get_client()

    # Create user in Supabase Auth
    auth_response = client.auth.admin.create_user({
        "email": email,
        "password": password,
        "email_confirm": True,  # Auto-confirm since we handle invite tokens
        "user_metadata": {"username": username},
    })

    user_id = auth_response.user.id

    # The trigger creates a basic profile; update it with admin level
    if level > 0:
        client.table("profiles").update({
            "is_admin": level,
        }).eq("id", user_id).execute()

    return user_id


def get_user_by_email(email: str) -> Optional[Row]:
    client = _get_client()
    result = client.table("profiles").select("*").eq("email", email.lower().strip()).limit(1).execute()
    if result.data:
        return _row(result.data[0])
    return None


def get_user_by_username(username: str) -> Optional[Row]:
    client = _get_client()
    result = client.table("profiles").select("*").eq("username", username.strip()).limit(1).execute()
    if result.data:
        return _row(result.data[0])
    return None


def get_user_by_email_or_username(identifier: str) -> Optional[Row]:
    """Look up a user by email or username."""
    identifier = identifier.strip()
    if "@" in identifier:
        return get_user_by_email(identifier)
    return get_user_by_username(identifier)


def get_user_by_id(user_id: str) -> Optional[Row]:
    client = _get_client()
    result = client.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    if result.data:
        return _row(result.data[0])
    return None


def set_default_dashboard(user_id: str, dashboard_key: Optional[str]) -> None:
    """Store the user's preferred landing dashboard (or clear it with None)."""
    client = _get_client()
    client.table("profiles").update({"default_dashboard": dashboard_key}).eq("id", user_id).execute()


def get_default_dashboard(user_id: str) -> Optional[str]:
    client = _get_client()
    result = client.table("profiles").select("default_dashboard").eq("id", user_id).limit(1).execute()
    if result.data:
        return result.data[0].get("default_dashboard")
    return None


def update_user_password(user_id: str, new_password: str) -> None:
    """Update a user's password via Supabase Auth admin API."""
    client = _get_client()
    client.auth.admin.update_user_by_id(user_id, {"password": new_password})


# ── Session operations ───────────────────────────────────────────────────────

SESSION_TTL = 30 * 24 * 60 * 60  # 30 days


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(48)
    now = int(time.time())
    client = _get_client()
    client.table("sessions").insert({
        "token": token,
        "user_id": user_id,
        "created_at": now,
        "expires_at": now + SESSION_TTL,
    }).execute()
    return token


def get_session(token: str) -> Optional[Row]:
    if not token:
        return None
    client = _get_client()
    now = int(time.time())
    # Join sessions with profiles
    result = client.table("sessions").select(
        "*, profiles!inner(username, email, is_admin)"
    ).eq("token", token).gt("expires_at", now).limit(1).execute()
    if not result.data:
        return None
    row = result.data[0]
    profile = row.get("profiles", {})
    return _row({
        "token": row["token"],
        "user_id": row["user_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "username": profile.get("username", ""),
        "email": profile.get("email", ""),
        "is_admin": profile.get("is_admin", 0),
    })


def delete_session(token: str) -> None:
    client = _get_client()
    client.table("sessions").delete().eq("token", token).execute()


def delete_user_sessions(user_id: str) -> None:
    """Delete all sessions for a user (used on password reset, suspension)."""
    client = _get_client()
    client.table("sessions").delete().eq("user_id", user_id).execute()


def purge_expired_sessions() -> int:
    client = _get_client()
    now = int(time.time())
    result = client.table("sessions").delete().lte("expires_at", now).execute()
    return len(result.data) if result.data else 0


# ── Subscription operations ─────────────────────────────────────────────────


def list_subscriptions(user_id: str) -> list[Row]:
    client = _get_client()
    result = client.table("subscriptions").select("*").eq("user_id", user_id).execute()
    return _rows(result.data)


def has_active_subscription(user_id: str, dashboard_key: str) -> bool:
    now = int(time.time())
    client = _get_client()

    # Admins bypass subscription checks
    profile = client.table("profiles").select("is_admin").eq("id", user_id).limit(1).execute()
    if profile.data and profile.data[0].get("is_admin"):
        return True

    result = client.table("subscriptions").select("id, expires_at").eq(
        "user_id", user_id
    ).eq("dashboard_key", dashboard_key).eq("status", "active").limit(1).execute()

    if not result.data:
        return False

    row = result.data[0]
    expires_at = row.get("expires_at")
    if expires_at is not None and expires_at <= now:
        return False
    return True


def upsert_subscription(
    user_id: str,
    dashboard_key: str,
    plan: str,
    duration_days: Optional[int] = None,
    source: str = "placeholder",
    stripe_sub_id: Optional[str] = None,
) -> None:
    now = int(time.time())
    expires_at = now + duration_days * 86400 if duration_days else None
    client = _get_client()
    client.table("subscriptions").upsert({
        "user_id": user_id,
        "dashboard_key": dashboard_key,
        "plan": plan,
        "status": "active",
        "started_at": now,
        "expires_at": expires_at,
        "stripe_sub_id": stripe_sub_id,
        "source": source,
    }, on_conflict="user_id,dashboard_key").execute()


def cancel_subscription(user_id: str, dashboard_key: str) -> None:
    client = _get_client()
    client.table("subscriptions").update(
        {"status": "cancelled"}
    ).eq("user_id", user_id).eq("dashboard_key", dashboard_key).execute()


# ── Invite token operations ─────────────────────────────────────────────────


def generate_invite_token() -> str:
    """Generate a 32-character URL-safe random invite token."""
    return secrets.token_urlsafe(24)


def create_invite_token(note: str = "", target_email: str = "") -> str:
    """Create a new unclaimed invite token. Returns the token string."""
    token = generate_invite_token()
    client = _get_client()
    client.table("invite_tokens").insert({
        "token": token,
        "status": "unclaimed",
        "note": note,
        "target_email": target_email.strip() or None,
        "created_at": int(time.time()),
    }).execute()
    return token


def get_invite_token(token: str) -> Optional[Row]:
    token = token.strip()
    client = _get_client()
    result = client.table("invite_tokens").select("*").eq("token", token).limit(1).execute()
    if result.data:
        return _row(result.data[0])
    return None


def claim_invite_token(token_str: str, user_id: str, email: str) -> bool:
    """Atomically claim a token. Returns True if claimed, False if already claimed."""
    token_str = token_str.strip()
    client = _get_client()

    # Only update if still unclaimed (atomic check)
    result = client.table("invite_tokens").update({
        "status": "claimed",
        "claimed_by_user_id": user_id,
        "claimed_by_email": email,
        "claimed_at": int(time.time()),
    }).eq("token", token_str).eq("status", "unclaimed").execute()

    if not result.data:
        return False

    # Link token to user profile
    token_row = client.table("invite_tokens").select("id").eq("token", token_str).limit(1).execute()
    if token_row.data:
        client.table("profiles").update({
            "invite_token_id": token_row.data[0]["id"]
        }).eq("id", user_id).execute()

    return True


def revoke_invite_token(token_id: int) -> None:
    client = _get_client()
    client.table("invite_tokens").update(
        {"status": "revoked"}
    ).eq("id", token_id).eq("status", "unclaimed").execute()


def list_invite_tokens() -> list[Row]:
    client = _get_client()
    result = client.table("invite_tokens").select("*").order("created_at", desc=True).execute()
    return _rows(result.data)


# ── User management (admin) ────────────────────────────────────────────────


def list_all_users() -> list[Row]:
    client = _get_client()
    result = client.table("profiles").select("*").order("created_at").execute()
    return _rows(result.data)


def set_user_role(user_id: str, level: int) -> None:
    """Set user role: 0=user, 1=admin, 2=super_admin."""
    client = _get_client()
    client.table("profiles").update({"is_admin": level}).eq("id", user_id).execute()


def set_user_admin(user_id: str, is_admin: bool) -> None:
    """Legacy helper — promotes to admin (1) or demotes to user (0)."""
    set_user_role(user_id, 1 if is_admin else 0)


def set_user_suspended(user_id: str, suspended: bool) -> None:
    client = _get_client()
    client.table("profiles").update(
        {"suspended": 1 if suspended else 0}
    ).eq("id", user_id).execute()
    if suspended:
        delete_user_sessions(user_id)


def update_user_email(user_id: str, new_email: str) -> None:
    """Update user email in both Supabase Auth and profiles."""
    client = _get_client()
    client.auth.admin.update_user_by_id(user_id, {"email": new_email})
    client.table("profiles").update({"email": new_email}).eq("id", user_id).execute()


def list_all_subscriptions() -> list[Row]:
    client = _get_client()
    result = client.table("subscriptions").select(
        "*, profiles!inner(email, username)"
    ).order("started_at", desc=True).execute()
    rows = []
    for r in result.data:
        profile = r.pop("profiles", {})
        r["email"] = profile.get("email", "")
        r["username"] = profile.get("username", "")
        rows.append(Row(r))
    return rows


def get_revenue_stats() -> dict:
    """Return subscription counts and breakdown by dashboard and plan."""
    now = int(time.time())
    client = _get_client()

    all_subs = client.table("subscriptions").select("*").execute().data

    total = len(all_subs)
    active = sum(
        1 for s in all_subs
        if s["status"] == "active" and (s["expires_at"] is None or s["expires_at"] > now)
    )
    cancelled = sum(1 for s in all_subs if s["status"] == "cancelled")
    expired = sum(
        1 for s in all_subs
        if s["status"] == "active" and s["expires_at"] is not None and s["expires_at"] <= now
    )

    # Per-dashboard active counts
    per_dashboard_map: dict[tuple[str, str], int] = {}
    for s in all_subs:
        if s["status"] == "active" and (s["expires_at"] is None or s["expires_at"] > now):
            key = (s["dashboard_key"], s["plan"])
            per_dashboard_map[key] = per_dashboard_map.get(key, 0) + 1

    per_dashboard = [
        Row({"dashboard_key": k[0], "plan": k[1], "cnt": v})
        for k, v in sorted(per_dashboard_map.items())
    ]

    return {
        "total": total,
        "active": active,
        "cancelled": cancelled,
        "expired": expired,
        "per_dashboard": per_dashboard,
    }


def create_enquiry(email: str, job_title: str, message: str) -> int:
    client = _get_client()
    result = client.table("enquiries").insert({
        "email": email.strip(),
        "job_title": job_title.strip(),
        "message": message.strip(),
        "created_at": int(time.time()),
    }).execute()
    return result.data[0]["id"] if result.data else 0


def list_enquiries() -> list[Row]:
    client = _get_client()
    result = client.table("enquiries").select("*").order("created_at", desc=True).execute()
    return _rows(result.data)


def get_enquiry_by_id(enquiry_id: int) -> Optional[Row]:
    client = _get_client()
    result = client.table("enquiries").select("*").eq("id", enquiry_id).limit(1).execute()
    if result.data:
        return _row(result.data[0])
    return None


def mark_enquiry_read(enquiry_id: int) -> None:
    client = _get_client()
    client.table("enquiries").update({"read": 1}).eq("id", enquiry_id).execute()


def count_unread_enquiries() -> int:
    client = _get_client()
    result = client.table("enquiries").select("id", count="exact").eq("read", 0).execute()
    return result.count if result.count is not None else 0


def get_stripe_customer_id(user_id: str) -> Optional[str]:
    client = _get_client()
    result = client.table("profiles").select("stripe_customer_id").eq("id", user_id).limit(1).execute()
    if result.data:
        return result.data[0].get("stripe_customer_id")
    return None


def set_stripe_customer_id(user_id: str, customer_id: str) -> None:
    client = _get_client()
    client.table("profiles").update({"stripe_customer_id": customer_id}).eq("id", user_id).execute()


def cancel_subscription_by_stripe_id(stripe_sub_id: str) -> None:
    """Cancel all subscriptions with the given Stripe subscription ID."""
    client = _get_client()
    client.table("subscriptions").update(
        {"status": "cancelled"}
    ).eq("stripe_sub_id", stripe_sub_id).execute()


def mask_email(email: str) -> str:
    """Mask email like sh***@gmail.com."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[:2]}***@{domain}"


# ── Password reset operations ──────────────────────────────────────────────

RESET_TTL = 60 * 60  # 1 hour


def create_password_reset(user_id: str) -> str:
    """Create a password reset token (expires in 1 hour). Returns the token."""
    token = secrets.token_urlsafe(36)
    now = int(time.time())
    client = _get_client()
    client.table("password_resets").insert({
        "user_id": user_id,
        "token": token,
        "created_at": now,
        "expires_at": now + RESET_TTL,
    }).execute()
    return token


def get_password_reset(token: str) -> Optional[Row]:
    """Get a valid (not expired, not used) password reset record."""
    if not token:
        return None
    now = int(time.time())
    client = _get_client()
    result = client.table("password_resets").select("*").eq(
        "token", token
    ).eq("used", 0).gt("expires_at", now).limit(1).execute()
    if result.data:
        return _row(result.data[0])
    return None


def use_password_reset(token: str) -> None:
    """Mark a reset token as used."""
    client = _get_client()
    client.table("password_resets").update({"used": 1}).eq("token", token).execute()


def purge_expired_resets() -> int:
    """Delete expired or used reset tokens."""
    now = int(time.time())
    client = _get_client()
    result = client.table("password_resets").delete().or_(
        f"expires_at.lte.{now},used.eq.1"
    ).execute()
    return len(result.data) if result.data else 0
