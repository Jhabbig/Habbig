"""Admin audit log — explicit logging of every admin action.

Principles:
  - Append-only. No delete endpoint exists anywhere.
  - NEVER raises: a failure in audit logging must not block the underlying
    admin action. Wrap everything in try/except and log at warning level.
  - Captures IP, user agent, and request_id (from LoggingContextMiddleware)
    from the FastAPI Request object passed in.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger("gateway.audit")


# ── Action constant catalog ──────────────────────────────────────────────────
# Full set per spec. Some are placeholders for features that don't have
# firing sites today (source.*, most scraper.*, most system.*) — they're
# defined so the filter dropdown has them when those features ship.


class AuditAction:
    # User management
    USER_VIEW               = "user.view"
    USER_SUSPEND            = "user.suspend"
    USER_UNSUSPEND          = "user.unsuspend"
    USER_PROMOTE_ADMIN      = "user.promote_admin"
    USER_DEMOTE_ADMIN       = "user.demote_admin"
    USER_ROLE_CHANGE        = "user.role_change"
    USER_EMAIL_CHANGE       = "user.email_change"
    USER_DELETE_INITIATED   = "user.delete_initiated"
    USER_DELETE_CANCELLED   = "user.delete_cancelled"
    USER_DELETE_COMPLETED   = "user.delete_completed"
    USER_GIFT_SUBSCRIPTION  = "user.gift_subscription"
    USER_REVOKE_GIFT        = "user.revoke_gift"
    USER_TRADING_ADDON      = "user.trading_addon"
    USER_BULK_ACTION        = "user.bulk_action"
    USER_EXPORT_DATA        = "user.export_data"

    # Token management
    TOKEN_GENERATE    = "token.generate"
    TOKEN_REVOKE      = "token.revoke"
    TOKEN_VIEW_LIST   = "token.view_list"

    # Source management (placeholders)
    SOURCE_TRUST_SET    = "source.trust_set"
    SOURCE_TRUST_UNSET  = "source.trust_unset"

    # Scraper management (placeholders)
    SCRAPER_TRIGGER        = "scraper.trigger"
    SCRAPER_PAUSE          = "scraper.pause"
    SCRAPER_RESUME         = "scraper.resume"
    SCRAPER_KEYWORD_ADD    = "scraper.keyword_add"
    SCRAPER_KEYWORD_REMOVE = "scraper.keyword_remove"
    SCRAPER_SESSION_RESET  = "scraper.session_reset"

    # System
    SYSTEM_PIPELINE_TRIGGER = "system.pipeline_trigger"
    SYSTEM_JOB_RETRY        = "system.job_retry"
    SYSTEM_CONFIG_CHANGE    = "system.config_change"

    # Forensic — reverse-lookup of a leaked email watermark to a user.
    # Hit OR miss is logged so the trail records the fingerprint an
    # admin attempted, not just successful resolutions.
    EMAIL_WATERMARK_TRACE   = "email.watermark_trace"

    # Auth (admin-specific)
    ADMIN_LOGIN       = "admin.login"
    ADMIN_LOGOUT      = "admin.logout"
    ADMIN_2FA_SETUP   = "admin.2fa_setup"
    ADMIN_2FA_DISABLE = "admin.2fa_disable"

    # Subproduct magic-link lifecycle (audit #15 CRIT #1 / MED #1).
    # MINT fires whenever the subproduct signup route signs a single-use
    # auth token bound to a user_id and embeds it in the Stripe Checkout
    # success URL. REDEEM fires when /onboarding burns the jti and mints
    # a session cookie. Together they let an admin reconstruct exactly
    # which user_id was authenticated by a magic link, who triggered the
    # mint, and from which IP — closing the prior "off-platform Stripe
    # redirect logs in user X with no audit trail" gap.
    MAGIC_LINK_MINT   = "magic_link.mint"
    MAGIC_LINK_REDEEM = "magic_link.redeem"

    # Feature flag CRUD (referenced by admin_routes.flag_create/save/delete).
    # These were missing — _audit() previously swallowed the AttributeError
    # and ZERO audit rows landed for any flag write. CRITICAL fix.
    FEATURE_FLAG_CREATE = "feature_flag.create"
    FEATURE_FLAG_UPDATE = "feature_flag.update"
    FEATURE_FLAG_DELETE = "feature_flag.delete"

    # Admin impersonation lifecycle. Same issue as feature flags above —
    # impersonate_start/end referenced these constants but the symbols
    # did not exist, so the audit trail had no record of any
    # impersonation session being opened or closed.
    IMPERSONATION_START   = "impersonation.start"
    IMPERSONATION_END     = "impersonation.end"
    # Logged by the impersonation middleware (gateway/server.py) when a
    # mutating verb is refused because the active impersonation session
    # is read-only. Without this constant the symbol-lookup raises
    # AttributeError which is swallowed in-place, so refused mutations
    # left no audit trail — the same class of bug the start/end constants
    # had. Defining it here makes the block surface as its own row.
    IMPERSONATION_BLOCKED = "impersonation.blocked"


ALL_ACTIONS = tuple(
    v for k, v in vars(AuditAction).items()
    if not k.startswith("_") and isinstance(v, str)
)

ACTION_LABELS = {
    AuditAction.USER_VIEW: "Viewed user profile",
    AuditAction.USER_SUSPEND: "Suspended user account",
    AuditAction.USER_UNSUSPEND: "Unsuspended user account",
    AuditAction.USER_PROMOTE_ADMIN: "Promoted user to admin",
    AuditAction.USER_DEMOTE_ADMIN: "Demoted user from admin",
    AuditAction.USER_ROLE_CHANGE: "Changed user role",
    AuditAction.USER_EMAIL_CHANGE: "Changed user email",
    AuditAction.USER_DELETE_INITIATED: "Initiated account deletion",
    AuditAction.USER_DELETE_CANCELLED: "Cancelled account deletion",
    AuditAction.USER_DELETE_COMPLETED: "Permanently deleted account",
    AuditAction.USER_GIFT_SUBSCRIPTION: "Gifted subscription",
    AuditAction.USER_REVOKE_GIFT: "Revoked gifted subscription",
    AuditAction.USER_TRADING_ADDON: "Granted trading add-on",
    AuditAction.USER_BULK_ACTION: "Bulk user action",
    AuditAction.USER_EXPORT_DATA: "Exported user data (admin GDPR shortcut)",
    AuditAction.TOKEN_GENERATE: "Generated access token",
    AuditAction.TOKEN_REVOKE: "Revoked access token",
    AuditAction.TOKEN_VIEW_LIST: "Viewed token list",
    AuditAction.SOURCE_TRUST_SET: "Set source trust flag",
    AuditAction.SOURCE_TRUST_UNSET: "Removed source trust flag",
    AuditAction.SCRAPER_TRIGGER: "Manually triggered scraper",
    AuditAction.SCRAPER_PAUSE: "Paused scraper job",
    AuditAction.SCRAPER_RESUME: "Resumed scraper job",
    AuditAction.SCRAPER_KEYWORD_ADD: "Added scraper keyword",
    AuditAction.SCRAPER_KEYWORD_REMOVE: "Removed scraper keyword",
    AuditAction.SCRAPER_SESSION_RESET: "Reset scraper session",
    AuditAction.SYSTEM_PIPELINE_TRIGGER: "Manually triggered pipeline",
    AuditAction.SYSTEM_JOB_RETRY: "Retried failed job",
    AuditAction.SYSTEM_CONFIG_CHANGE: "Changed system configuration",
    AuditAction.EMAIL_WATERMARK_TRACE: "Traced email watermark to recipient",
    AuditAction.ADMIN_LOGIN: "Admin login",
    AuditAction.ADMIN_LOGOUT: "Admin logout",
    AuditAction.ADMIN_2FA_SETUP: "Set up 2FA",
    AuditAction.ADMIN_2FA_DISABLE: "Disabled 2FA",
    AuditAction.FEATURE_FLAG_CREATE: "Created feature flag",
    AuditAction.FEATURE_FLAG_UPDATE: "Updated feature flag",
    AuditAction.FEATURE_FLAG_DELETE: "Deleted feature flag",
    AuditAction.IMPERSONATION_START: "Started impersonation session",
    AuditAction.IMPERSONATION_END: "Ended impersonation session",
    AuditAction.IMPERSONATION_BLOCKED: "Blocked mutating action during impersonation",
    AuditAction.MAGIC_LINK_MINT: "Minted subproduct magic-link token",
    AuditAction.MAGIC_LINK_REDEEM: "Redeemed subproduct magic-link token",
}


# ── User snapshot (for before/after) ─────────────────────────────────────────


_SNAPSHOT_FIELDS = (
    "id",
    "username",
    "email",
    "is_admin",
    "suspended",
    "invite_token_id",
    "two_fa_method",
    "deletion_requested_at",
    "is_deleted",
)


def snapshot_user(user_id: int) -> Optional[dict]:
    """Return a minimal dict capturing the mutable admin-visible fields of a user.

    Used for before/after JSON capture in audit log entries. Keys match the
    column names in the users table.
    """
    try:
        import db
        row = db.get_user_by_id(user_id)
        if not row:
            return None
        out: dict = {}
        for key in _SNAPSHOT_FIELDS:
            try:
                out[key] = row[key]
            except (KeyError, IndexError):
                pass
        return out
    except Exception as e:
        log.warning("snapshot_user failed for user_id=%s: %s", user_id, e)
        return None


# ── Main log helper ──────────────────────────────────────────────────────────


def _get_ip(request) -> str:
    """Best-effort client IP via the canonical ``server._get_client_ip``.

    Audit MED FIX (audit_security_dir.md cross-cutting): three different
    ``get_client_ip`` implementations had drifted across ``audit.py``,
    ``logger.py``, and ``rate_limiter.py``. ``audit.py`` was the outlier
    — it never honoured ``cf-connecting-ip``, so admin actions on a
    Cloudflare-fronted path recorded the loopback peer instead of the
    real client. Delegate to ``server._get_client_ip`` (the canonical
    trusted-proxy-gated helper) so the audit trail matches every other
    log line in the gateway.

    Deferred import: ``security/`` is imported by ``server`` at module
    load, so a top-level ``from server import _get_client_ip`` would
    cycle. Inline import resolves at call time, after ``server`` has
    finished loading.
    """
    if request is None:
        return ""
    try:
        from server import _get_client_ip as _server_get_client_ip
        ip = _server_get_client_ip(request)
        # server._get_client_ip returns "unknown" when neither peer nor
        # trusted headers are available; the audit ledger has historically
        # stored empty string for that case so callers reading
        # ip_address.NULL get the same evidence shape.
        return "" if ip == "unknown" else ip
    except Exception:
        # Same fallback shape as before — never raise from the audit path.
        try:
            xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            if xff:
                return xff
            if getattr(request, "client", None):
                return request.client.host or ""
        except Exception:
            pass
        return ""


def _get_user_agent(request) -> str:
    if request is None:
        return ""
    try:
        return (request.headers.get("user-agent") or "")[:500]
    except Exception:
        return ""


def _get_request_id(request) -> str:
    if request is None:
        return ""
    try:
        return request.headers.get("x-request-id") or ""
    except Exception:
        return ""


def _to_json(value) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except Exception:
        return None


def log_action(
    *,
    admin_user_id: Optional[int],
    admin_email: Optional[str],
    action: str,
    target_type: Optional[str] = None,
    target_id=None,
    target_description: Optional[str] = None,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    request=None,
    notes: Optional[str] = None,
) -> None:
    """Write one row to audit_log. NEVER raises.

    Caller passes before/after as plain dicts; this function JSON-serializes
    them. Passes request through to extract IP/user-agent/request-id.
    """
    try:
        import db
        db.insert_audit_log(
            admin_user_id=admin_user_id,
            admin_email=admin_email,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            target_description=target_description,
            before_state=_to_json(before),
            after_state=_to_json(after),
            ip_address=_get_ip(request),
            user_agent=_get_user_agent(request),
            request_id=_get_request_id(request),
            notes=notes,
        )
    except Exception as e:
        log.warning("audit.log_action failed (%s): %s", action, e)


def log_admin_action(admin_user: dict, action: str, request=None, **kwargs) -> None:
    """Convenience wrapper when the caller already has the current admin dict.

    admin_user is the dict returned by server.current_user() — has user_id+email.
    """
    if not admin_user:
        return
    log_action(
        admin_user_id=admin_user.get("user_id"),
        admin_email=admin_user.get("email"),
        action=action,
        request=request,
        **kwargs,
    )


# ── Filter helper for admin audit log page / CSV export ──────────────────────


def filter_to_query_kwargs(query_params) -> dict:
    """Translate query params (QueryParams-like) into kwargs for db.query_audit_log.

    Accepts: action, admin_id, target_type, from, to (all optional).
    Dates may be passed as YYYY-MM-DD (local) — converted to unix timestamps.
    """
    import time as _time

    def _parse_date(value: str, end_of_day: bool = False) -> Optional[int]:
        value = (value or "").strip()
        if not value:
            return None
        try:
            tm = _time.strptime(value, "%Y-%m-%d")
            ts = int(_time.mktime(tm))
            if end_of_day:
                ts += 86399
            return ts
        except (ValueError, TypeError):
            return None

    def _get(key: str) -> str:
        try:
            return (query_params.get(key) or "").strip()
        except Exception:
            return ""

    kwargs: dict = {}
    action = _get("action")
    if action:
        kwargs["action"] = action
    admin_id = _get("admin_id")
    if admin_id.isdigit():
        kwargs["admin_user_id"] = int(admin_id)
    target_type = _get("target_type")
    if target_type:
        kwargs["target_type"] = target_type
    from_ts = _parse_date(_get("from"))
    if from_ts:
        kwargs["from_ts"] = from_ts
    to_ts = _parse_date(_get("to"), end_of_day=True)
    if to_ts:
        kwargs["to_ts"] = to_ts
    return kwargs


def filter_to_search_kwargs(query_params) -> dict:
    """v2 of `filter_to_query_kwargs` that also surfaces admin_email +
    target_user_id, and resolves the "quick chip" range parameter
    (``range=24h|7d|30d|today``) into a from_ts/to_ts pair.

    The legacy helper above is preserved untouched because the older
    /admin/audit-log call sites (and any third-party scripts importing it
    via `db.query_audit_log`) shouldn't sprout new behaviour on this
    polish pass.
    """
    import time as _time

    def _parse_date(value: str, end_of_day: bool = False) -> Optional[int]:
        value = (value or "").strip()
        if not value:
            return None
        try:
            tm = _time.strptime(value, "%Y-%m-%d")
            ts = int(_time.mktime(tm))
            if end_of_day:
                ts += 86399
            return ts
        except (ValueError, TypeError):
            return None

    def _get(key: str) -> str:
        try:
            return (query_params.get(key) or "").strip()
        except Exception:
            return ""

    kwargs: dict = {}

    action = _get("action")
    if action and action != "all":
        kwargs["action"] = action

    admin_id = _get("admin_id")
    if admin_id.isdigit():
        kwargs["admin_user_id"] = int(admin_id)

    admin_email = _get("admin_email")
    if admin_email:
        kwargs["admin_email"] = admin_email

    target_type = _get("target_type")
    if target_type:
        kwargs["target_type"] = target_type

    target_user_id = _get("target_user_id")
    if target_user_id:
        kwargs["target_user_id"] = target_user_id

    # Quick-chip range overrides explicit from/to so the chip behaves as
    # a "snap to now" shortcut. The chip is mutually exclusive with the
    # date inputs by JS, but server-side we honour from/to first when
    # the chip is empty so a deep-link URL with from=YYYY-MM-DD still works.
    rng = _get("range").lower()
    now = int(_time.time())
    chip_from = None
    if rng == "today":
        tm = _time.localtime(now)
        chip_from = int(_time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1)))
    elif rng in ("24h", "1d"):
        chip_from = now - 86400
    elif rng == "7d":
        chip_from = now - 7 * 86400
    elif rng == "30d":
        chip_from = now - 30 * 86400

    if chip_from is not None:
        kwargs["from_ts"] = chip_from
        kwargs["to_ts"] = now
    else:
        from_ts = _parse_date(_get("from"))
        if from_ts:
            kwargs["from_ts"] = from_ts
        to_ts = _parse_date(_get("to"), end_of_day=True)
        if to_ts:
            kwargs["to_ts"] = to_ts

    return kwargs
