"""Build a per-user GDPR ZIP export.

Each export captures every user-linked row in the gateway DB, written
both as CSV (Excel/Sheets-friendly) and JSON (machine-readable) so the
export is portable to any other system.

The ZIP layout is documented in README.txt (also written into the ZIP),
and matches the spec under PR description verbatim:

    narve-data-export-{user_id}-{timestamp}.zip
    ├── README.txt
    ├── account.json
    ├── subscriptions.json
    ├── predictions/saved.{csv,json}
    ├── markets/viewed.{csv,json}
    ├── sources/followed.{csv,json}
    ├── signal_search/topics.json
    ├── intelligence/conversations/conversation-N.md
    ├── notifications/history.csv
    ├── activity/login_history.csv
    ├── trading/positions.{csv,json}                ← audit #4 carryover
    ├── trading/connections.{csv,json}              ← metadata only
    ├── whale/watchlist.{csv,json}
    ├── social/{takes,votes,follows,collections,...}
    └── metadata.json

Storage: ZIPs land under EXPORT_DIR (default /tmp/narve-exports/) and
live for EXPORT_TTL_SECONDS (default 7 days). Downloads are gated by an
HMAC signature; the file path itself is never exposed.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("exports.generator")


# ── Storage + signed URL config ──────────────────────────────────────────────

EXPORT_DIR = Path(
    os.environ.get("DATA_EXPORT_DIR", str(Path.home() / ".narve" / "exports"))
)
# Ensure the export directory exists with restrictive perms so other local
# users can't read another user's GDPR ZIP. mode= only takes effect at
# creation time, so we also chmod the dir in case it already existed with
# looser perms (e.g. from an earlier /tmp default).
try:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(EXPORT_DIR, 0o700)
except OSError as _exc:
    log.warning("exports: could not secure EXPORT_DIR %s: %s", EXPORT_DIR, _exc)

EXPORT_TTL_SECONDS = int(os.environ.get("DATA_EXPORT_TTL_SECONDS", str(7 * 24 * 3600)))
APP_URL = os.environ.get("APP_URL", "https://narve.ai").rstrip("/")
PRIVACY_EMAIL = os.environ.get("PRIVACY_EMAIL", "privacy@narve.ai")

_SIGNING_SECRET_FALLBACK_WARNED = False


def _signing_secret() -> bytes:
    """Read the HMAC secret at call time so tests can override env vars.

    Prefers a dedicated ``DATA_EXPORT_SIGNING_SECRET`` so that rotating the
    session cookie secret doesn't silently invalidate in-flight download
    links (and vice versa). Falls back to ``GATEWAY_COOKIE_SECRET`` for
    backwards compatibility, emitting a one-shot warning so operators see
    the migration nudge.
    """
    global _SIGNING_SECRET_FALLBACK_WARNED
    secret = os.environ.get("DATA_EXPORT_SIGNING_SECRET", "").strip()
    if not secret:
        secret = os.environ.get("GATEWAY_COOKIE_SECRET", "").strip()
        if secret and not _SIGNING_SECRET_FALLBACK_WARNED:
            log.warning(
                "exports: DATA_EXPORT_SIGNING_SECRET not set; falling back to "
                "GATEWAY_COOKIE_SECRET. Set a dedicated secret to decouple "
                "download-link signing from session cookies."
            )
            _SIGNING_SECRET_FALLBACK_WARNED = True
    if not secret:
        # Fail loudly — a default secret would let an attacker forge URLs.
        raise RuntimeError(
            "DATA_EXPORT_SIGNING_SECRET (or GATEWAY_COOKIE_SECRET fallback) "
            "is required for signed download URLs"
        )
    return secret.encode()


def sign_download_url(export_id: int, expires_at: int) -> str:
    """Return an absolute URL the user can hit to download the ZIP.

    The token binds (export_id, expires_at) so an attacker can't replay
    one user's link to download another user's file or extend their own
    link's lifetime.
    """
    msg = f"{export_id}:{expires_at}".encode()
    sig = hmac.new(_signing_secret(), msg, hashlib.sha256).hexdigest()
    return (
        f"{APP_URL}/api/account/export/{export_id}/download"
        f"?expires={expires_at}&token={sig}"
    )


def verify_download_token(export_id: int, expires_at: int, token: str) -> bool:
    """Constant-time check + expiry check. Returns True iff both pass."""
    if not token:
        return False
    if expires_at < int(time.time()):
        return False
    msg = f"{export_id}:{expires_at}".encode()
    expected = hmac.new(_signing_secret(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, token)


# ── Per-table fetchers ───────────────────────────────────────────────────────


def _row_to_dict(row: Optional[sqlite3.Row]) -> dict:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


def _rows_to_dicts(rows) -> list[dict]:
    return [_row_to_dict(r) for r in rows]


_FROM_TABLE_RE = re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def _extract_table_name(sql: str) -> str:
    """Best-effort first table referenced in a SELECT.

    Used purely for log/manifest annotation — we'd rather emit the
    table we *tried* to read than a SQL fragment. Joined queries get
    the first FROM target; unparseable strings fall back to "<unknown>".
    """
    m = _FROM_TABLE_RE.search(sql or "")
    return m.group(1) if m else "<unknown>"


def _safe_query(
    conn,
    sql: str,
    params: tuple = (),
    errors: Optional[list[dict]] = None,
) -> list[dict]:
    """Run a raw SELECT and return list[dict].

    Returns [] when the referenced table or column does not exist — keeps
    the export resilient against schemas the user happens not to have
    rows in (a test DB built from db.py won't have every migration's
    tables, and schema drift across deploys would otherwise break the
    whole export over a single column rename).

    Schema-drift misses are NOT silent: each one logs a warning and (if
    *errors* is provided) appends a manifest entry so the operator and
    the data subject both know the export skipped a table instead of
    pretending it had no rows. Any other ``OperationalError`` (syntax
    issue, locked DB, real bug) re-raises so it surfaces upstream
    instead of producing a deceptively-complete archive.
    """
    try:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "no such table" in msg or "no such column" in msg:
            table = _extract_table_name(sql)
            log.warning("export query failed: table=%s err=%s", table, e)
            if errors is not None:
                errors.append({"table": table, "reason": str(e)})
            return []
        raise


def _scrub_user_row(row: dict) -> dict:
    """Drop password material before serializing the user row."""
    drop = {"password_hash", "password_salt"}
    return {k: v for k, v in row.items() if k not in drop}


def _scrub_market_credential_row(row: dict) -> dict:
    """Strip encrypted tokens and wallet addresses from connection rows.

    GDPR export must surface that the user *has* a Kalshi / Polymarket
    connection (source + connected_at + last_used_at), but the encrypted
    token or wallet address itself is sensitive and would let a stolen
    export blob unlock the user's brokerage account. We retain identity-
    less metadata only — see the inline note in the export bundle for
    operators and end users.
    """
    safe_keys = {
        "id", "source", "connected_at", "last_used_at",
        "is_active", "kalshi_member_id", "token_expires_at",
        "kalshi_token_expires_at", "last_synced_at",
        "sync_error_count",
    }
    return {k: v for k, v in row.items() if k in safe_keys}


def _scrub_session_row(row: dict) -> dict:
    """Keep timing/UA, drop any token hash or raw token if present.

    The hardened user_sessions table never stores the raw token, only a
    SHA-256 hash. We strip that hash plus any legacy raw-token column —
    a hash leak is still a leak (attacker can mass-revoke or correlate
    against captured network logs)."""
    drop = {"token_hash", "legacy_token", "csrf_token"}
    return {k: v for k, v in row.items() if k not in drop}


def _scrub_audit_row(row: dict) -> dict:
    """Audit log entries that target this user.

    GDPR Art. 15 right-of-access covers admin actions taken against the
    data subject (suspensions, role changes, manual data edits). We keep
    action / timestamp / target_id / target_description / before-after,
    but redact the admin's IP + UA — those are the *admin's* personal
    data, not the user's, and would leak through if we did SELECT *.
    """
    drop = {"ip_address", "user_agent", "request_id", "admin_email"}
    return {k: v for k, v in row.items() if k not in drop}


def _collect(user_id: int) -> dict[str, Any]:
    """Gather every user-linked row keyed by export-bundle name.

    Returns a flat dict where each value is either a dict (single row) or
    a list[dict] (table). Keys map 1:1 to filenames inside the ZIP.

    Schema-drift errors raised inside ``_safe_query`` accumulate into
    ``bundle["__errors__"]`` so ``build_zip`` can surface them in the
    export manifest. The leading underscores keep the key out of the
    zip's file map (build_zip iterates a fixed list of bundle keys).
    """
    import db

    errors: list[dict] = []

    # Local _safe_query wrapper that pins the shared error list so every
    # call site below routes schema-drift misses into the manifest without
    # having to thread ``errors=`` through dozens of call signatures. We
    # grab the module-level implementation via sys.modules to dodge the
    # name-shadowing rule (binding ``_safe_query`` below makes the bare
    # name local for the whole function, so a direct global lookup would
    # raise UnboundLocalError).
    _module_safe_query = sys.modules[__name__]._safe_query

    def _safe_query(conn, sql, params=()):  # noqa: E306, F811
        return _module_safe_query(conn, sql, params, errors=errors)

    bundle: dict[str, Any] = {"__errors__": errors}
    with db.conn() as c:
        # Account profile
        user_row = c.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        bundle["account"] = _scrub_user_row(_row_to_dict(user_row))

        # Subscriptions / billing
        bundle["subscriptions"] = _safe_query(
            c,
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY started_at DESC",
            (user_id,),
        )

        # Saved predictions (joined for richer export)
        bundle["saved_predictions"] = _safe_query(
            c,
            "SELECT sp.id AS saved_id, sp.saved_at, sp.notes, "
            "sp.notified_on_resolution, p.id AS prediction_id, "
            "p.source_handle, p.content, p.direction, p.market_id, "
            "p.predicted_probability, p.extracted_at, p.resolved, "
            "p.resolved_correct "
            "FROM saved_predictions sp "
            "LEFT JOIN predictions p ON p.id = sp.prediction_id "
            "WHERE sp.user_id = ? ORDER BY sp.saved_at DESC",
            (user_id,),
        )

        # Markets viewed
        bundle["viewed_markets"] = _safe_query(
            c,
            "SELECT * FROM user_market_views WHERE user_id = ? "
            "ORDER BY last_viewed_at DESC",
            (user_id,),
        )

        # Sources followed
        bundle["followed_sources"] = _safe_query(
            c,
            "SELECT * FROM followed_sources WHERE user_id = ? "
            "ORDER BY followed_at DESC",
            (user_id,),
        )

        # Signal Search topics
        bundle["topics"] = _safe_query(
            c,
            "SELECT * FROM user_topics WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )

        # Intelligence conversations + messages
        convs = _safe_query(
            c,
            "SELECT * FROM intelligence_conversations WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )
        bundle["conversations"] = convs
        msgs_by_conv: dict[int, list[dict]] = {}
        for conv in convs:
            msgs_by_conv[conv["id"]] = _safe_query(
                c,
                "SELECT * FROM intelligence_messages "
                "WHERE conversation_id = ? ORDER BY created_at ASC",
                (conv["id"],),
            )
        bundle["conversation_messages"] = msgs_by_conv

        # User-owned alert rules (the new market_movements ones, if migrated)
        bundle["market_alerts"] = _safe_query(
            c,
            "SELECT * FROM user_market_alerts WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )

        # Notification history — sent emails for this user, best-effort.
        # The `email_send_log` and `saved_predictions.notified_on_resolution`
        # tables are the closest thing we have to "notifications sent".
        bundle["notifications"] = _safe_query(
            c,
            "SELECT * FROM email_send_log WHERE user_id = ? "
            "ORDER BY sent_at DESC LIMIT 1000",
            (user_id,),
        )

        # Activity / login history — the hardened session table records
        # IP + UA per session start. Session token hash never leaves the
        # DB (see _scrub_session_row); explicit allow-list keeps any
        # future column from leaking into the export by accident.
        bundle["sessions"] = [
            _scrub_session_row(r)
            for r in _safe_query(
                c,
                "SELECT id, user_id, created_at, expires_at, last_active_at, "
                "ip_address, user_agent, revoked, revoked_at "
                "FROM user_sessions WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        ]

        # Bet history (trading add-on)
        bundle["bet_history"] = _safe_query(
            c,
            "SELECT * FROM user_bet_history WHERE user_id = ? "
            "ORDER BY placed_at DESC",
            (user_id,),
        )

        # API keys (metadata only — never the key itself)
        bundle["api_keys"] = _safe_query(
            c,
            "SELECT id, key_prefix, name, tier, rate_limit_hour, "
            "created_at, last_used_at, revoked_at "
            "FROM api_keys WHERE user_id = ?",
            (user_id,),
        )

        # Telegram link
        bundle["telegram_links"] = _safe_query(
            c,
            "SELECT * FROM telegram_user_links WHERE user_id = ?",
            (user_id,),
        )

        # Email unsubscribes
        bundle["email_unsubscribes"] = _safe_query(
            c,
            "SELECT * FROM email_unsubscribes WHERE user_id = ?",
            (user_id,),
        )

        # Backtests
        bundle["backtests"] = _safe_query(
            c,
            "SELECT * FROM backtests WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )

        # Feedback submitted
        bundle["feedback"] = _safe_query(
            c,
            "SELECT * FROM feedback_submissions WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )

        # Gifts received
        bundle["gifted_subscriptions"] = _safe_query(
            c,
            "SELECT * FROM gifted_subscriptions WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )

        # ── Subproduct-scoped data (added 2026-05-14, audit #4 follow-up) ──
        # Trading add-on: open + closed positions across Polymarket / Kalshi.
        # This was the headline carryover from audit #4 — without it the
        # export was missing every position the user has ever held.
        bundle["user_positions"] = _safe_query(
            c,
            "SELECT * FROM user_positions WHERE user_id = ? "
            "ORDER BY last_synced_at DESC",
            (user_id,),
        )

        # User's own predictions (the parallel pipeline from migration 031
        # keyed by user_id, not source_handle).
        bundle["user_predictions"] = _safe_query(
            c,
            "SELECT * FROM user_predictions WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )
        bundle["user_prediction_stats"] = _safe_query(
            c,
            "SELECT * FROM user_prediction_stats WHERE user_id = ?",
            (user_id,),
        )

        # Trading add-on settings (Kelly fraction / bankroll prefs / risk caps
        # — added today as part of /settings/integrations).
        bundle["user_trading_addon_settings"] = _safe_query(
            c,
            "SELECT * FROM user_trading_addon_settings WHERE user_id = ?",
            (user_id,),
        )

        # Market credentials — REDACTED to metadata only.
        # The encrypted Kalshi token + Polymarket wallet hash are NEVER
        # exported (see _scrub_market_credential_row). We list the
        # connection so the user can see which platforms they've linked,
        # but never the secret material itself.
        # Tokens redacted for security; connection metadata only.
        bundle["user_market_credentials"] = [
            _scrub_market_credential_row(r)
            for r in _safe_query(
                c,
                "SELECT id, source, connected_at, last_used_at, is_active, "
                "kalshi_member_id, kalshi_token_expires_at "
                "FROM user_market_credentials WHERE user_id = ? "
                "ORDER BY connected_at DESC",
                (user_id,),
            )
        ]
        # Newer polymarket/kalshi connection tables (migration 062) — same
        # redaction rule applies. We surface connected_at + sync timing
        # but never the encrypted_token or the wallet address itself.
        bundle["polymarket_connections"] = [
            _scrub_market_credential_row({**r, "source": "polymarket"})
            for r in _safe_query(
                c,
                "SELECT id, connected_at, last_synced_at, sync_error_count "
                "FROM polymarket_connections WHERE user_id = ?",
                (user_id,),
            )
        ]
        bundle["kalshi_connections"] = [
            _scrub_market_credential_row({**r, "source": "kalshi"})
            for r in _safe_query(
                c,
                "SELECT id, connected_at, last_synced_at, token_expires_at, "
                "member_id AS kalshi_member_id, sync_error_count "
                "FROM kalshi_connections WHERE user_id = ?",
                (user_id,),
            )
        ]

        # Whale-dashboard watchlist (per-user followed tickers + filers).
        # Lives in a separate sqlite DB on production; the gateway connects
        # to it lazily and the _safe_query fall-through means tests without
        # the whale schema don't break.
        bundle["whale_watchlist"] = _safe_query(
            c,
            "SELECT * FROM whale_watchlist WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )

        # In-app notifications feed (migration 026).
        bundle["notifications_feed"] = _safe_query(
            c,
            "SELECT * FROM notifications WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 5000",
            (user_id,),
        )
        bundle["notification_preferences"] = _safe_query(
            c,
            "SELECT * FROM notification_preferences WHERE user_id = ?",
            (user_id,),
        )
        bundle["push_subscriptions"] = _safe_query(
            c,
            "SELECT id, endpoint, user_agent, created_at, last_used_at "
            "FROM push_subscriptions WHERE user_id = ?",
            (user_id,),
        )

        # Engagement / onboarding state — what the user has interacted with.
        bundle["engagement_events"] = _safe_query(
            c,
            "SELECT * FROM engagement_events WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 5000",
            (user_id,),
        )
        bundle["user_onboarding"] = _safe_query(
            c,
            "SELECT * FROM user_onboarding WHERE user_id = ?",
            (user_id,),
        )
        bundle["user_first_week_goals"] = _safe_query(
            c,
            "SELECT * FROM user_first_week_goals WHERE user_id = ?",
            (user_id,),
        )
        bundle["changelog_seen"] = _safe_query(
            c,
            "SELECT * FROM changelog_seen WHERE user_id = ?",
            (user_id,),
        )

        # Public-facing user content: takes, votes, follows, collections.
        bundle["market_takes"] = _safe_query(
            c,
            "SELECT * FROM market_takes WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )
        bundle["take_votes"] = _safe_query(
            c,
            "SELECT * FROM take_votes WHERE user_id = ?",
            (user_id,),
        )
        bundle["user_follows"] = _safe_query(
            c,
            "SELECT * FROM user_follows WHERE follower_user_id = ? "
            "OR followed_user_id = ?",
            (user_id, user_id),
        )
        bundle["collections"] = _safe_query(
            c,
            "SELECT * FROM collections WHERE owner_user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )
        bundle["collection_follows"] = _safe_query(
            c,
            "SELECT * FROM collection_follows WHERE user_id = ?",
            (user_id,),
        )
        bundle["saved_views"] = _safe_query(
            c,
            "SELECT * FROM saved_views WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )

        # Webhooks — REDACTED secret (HMAC signing secret).
        bundle["webhook_subscriptions"] = _safe_query(
            c,
            "SELECT id, url, events, created_at, is_active, last_delivered_at, "
            "failure_count, consecutive_failures "
            "FROM webhook_subscriptions WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )

        # Public feedback board (separate from feedback_submissions which
        # is the older one-off form). feedback_items uses ON DELETE SET NULL
        # so deletions preserve the public thread anonymously.
        bundle["feedback_items"] = _safe_query(
            c,
            "SELECT * FROM feedback_items WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )
        bundle["feedback_votes"] = _safe_query(
            c,
            "SELECT * FROM feedback_votes WHERE user_id = ?",
            (user_id,),
        )
        bundle["feedback_comments"] = _safe_query(
            c,
            "SELECT * FROM feedback_comments WHERE user_id = ? "
            "ORDER BY created_at",
            (user_id,),
        )

        # Subscription lifecycle — cancellation / pause history.
        bundle["cancellation_attempts"] = _safe_query(
            c,
            "SELECT * FROM cancellation_attempts WHERE user_id = ? "
            "ORDER BY started_at DESC",
            (user_id,),
        )
        bundle["subscription_pauses"] = _safe_query(
            c,
            "SELECT * FROM subscription_pauses WHERE user_id = ? "
            "ORDER BY started_at DESC",
            (user_id,),
        )

        # Invite tokens the user has issued (referral system).
        bundle["user_invite_tokens"] = _safe_query(
            c,
            "SELECT id, tier_at_grant, created_at, used_at, used_by_user_id, "
            "is_active, source FROM user_invite_tokens WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )
        # Referral edges where the user is either side.
        bundle["referrals"] = _safe_query(
            c,
            "SELECT * FROM referrals "
            "WHERE referrer_user_id = ? OR referred_user_id = ? "
            "ORDER BY created_at DESC",
            (user_id, user_id),
        )

        # Affiliate program enrolment.
        bundle["affiliate_accounts"] = _safe_query(
            c,
            "SELECT * FROM affiliate_accounts WHERE user_id = ?",
            (user_id,),
        )

        # Weekly performance reports the user has been emailed.
        bundle["weekly_reports"] = _safe_query(
            c,
            "SELECT * FROM weekly_reports WHERE user_id = ? "
            "ORDER BY period_start DESC",
            (user_id,),
        )

        # Discord / extended Telegram connections.
        bundle["discord_user_connections"] = _safe_query(
            c,
            "SELECT * FROM discord_user_connections WHERE user_id = ?",
            (user_id,),
        )
        bundle["telegram_connections"] = _safe_query(
            c,
            "SELECT * FROM telegram_connections WHERE user_id = ?",
            (user_id,),
        )

        # Embed widgets the user has created.
        bundle["embed_widgets"] = _safe_query(
            c,
            "SELECT * FROM embed_widgets WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        )

        # GDPR self-service log — the user's own past export requests.
        bundle["data_export_requests"] = _safe_query(
            c,
            "SELECT id, requested_at, completed_at, expires_at, status, "
            "file_size_bytes FROM data_export_requests WHERE user_id = ? "
            "ORDER BY requested_at DESC",
            (user_id,),
        )

        # Admin actions where the user is the target (Art. 15 right of
        # access — the user can see what admins have done to their data).
        # IP / UA / request_id stripped: those are the admin's PII, not
        # the user's, and would leak through if we did SELECT *.
        bundle["audit_log"] = [
            _scrub_audit_row(r)
            for r in _safe_query(
                c,
                "SELECT id, timestamp, action, target_type, target_id, "
                "target_description, before_state, after_state, notes "
                "FROM audit_log WHERE target_type = 'user' AND target_id = ? "
                "ORDER BY timestamp DESC LIMIT 1000",
                (str(user_id),),
            )
        ]

    return bundle


# ── Format helpers ──────────────────────────────────────────────────────────


def _to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    # Union of keys across all rows so a sparse column doesn't disappear.
    fieldnames: list[str] = []
    seen: set = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        # Stringify any nested dicts/lists so csv doesn't blow up.
        row_out = {}
        for k, v in r.items():
            if isinstance(v, (dict, list)):
                row_out[k] = json.dumps(v, default=str)
            else:
                row_out[k] = v
        writer.writerow(row_out)
    return buf.getvalue()


def _to_json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str, sort_keys=True)


def _conversation_to_markdown(conv: dict, messages: list[dict]) -> str:
    """Render a single Intelligence conversation as readable Markdown."""
    title = conv.get("title") or f"Conversation {conv.get('id')}"
    created = conv.get("created_at")
    if isinstance(created, (int, float)):
        created_iso = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
    else:
        created_iso = str(created or "unknown")
    out = [f"# {title}", "", f"_Created: {created_iso}_", ""]
    for m in messages:
        role = (m.get("role") or "?").capitalize()
        ts = m.get("created_at")
        if isinstance(ts, (int, float)):
            ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        else:
            ts_iso = str(ts or "")
        out.append(f"## {role} — {ts_iso}")
        out.append("")
        content = m.get("content") or ""
        # Don't break Markdown by interpreting user content as Markdown
        # syntax — wrap multi-paragraph content in a fenced block when it
        # looks like code, otherwise emit as plain paragraphs.
        out.append(content.strip())
        out.append("")
    return "\n".join(out)


def _readme(user_email: str, exported_at_iso: str) -> str:
    return f"""NARVE.AI DATA EXPORT

Exported: {exported_at_iso}
For account: {user_email}

This archive contains all data narve.ai has associated with your
account.

CONTENTS

  account.json          Your profile and preferences
  subscriptions.json    Current and past subscriptions
  predictions/          All saved predictions + your own predictions (CSV and JSON)
  markets/              Markets you've viewed
  sources/              Sources you follow
  signal_search/        Your Signal Search topics
  intelligence/         Your AI assistant conversations (Markdown, one per file)
  notifications/        Notification history + in-app feed + preferences
  activity/             Login history, engagement events, onboarding, audit log
  trading/              Positions, bet history, backtests, broker connections (metadata only)
  social/               Takes, votes, follows, collections, saved views, referrals
  developer/            API keys, webhook subscriptions, embed widgets
  whale/                Whale-dashboard watchlist
  reports/              Weekly performance reports
  integrations/         Telegram / Discord connections
  feedback/             Feedback you've submitted, voted on, or commented
  billing/              Gifts, affiliate accounts, cancellation / pause history
  metadata.json         Export manifest (file list, row counts, schema versions)

SECURITY-SENSITIVE FIELDS — REDACTED

The following fields are NOT included in this export so that a leaked
archive cannot be used to compromise your account or your brokerage:

  - Password hash / salt
  - Kalshi encrypted bearer token + Polymarket wallet private material
    (the connections themselves appear under trading/, but only the
    platform name, connected_at, and last_used_at — never the secret)
  - Session token hashes (your login history shows when, from where,
    and which device — but never the cryptographic token itself)
  - Admin IP addresses + user-agents from audit-log entries about your
    account (those are the admin's personal data, not yours)
  - Webhook signing secrets

If you need any of the above for a legal or law-enforcement request,
contact {PRIVACY_EMAIL} directly with proper authorization.

FORMATS

  CSV files: compatible with Excel, Google Sheets, Numbers
  JSON files: machine-readable, includes all metadata

DELETION

  If you want to delete your account entirely, visit Settings → Privacy.
  Export data is retained in our system for 7 days, then deleted.

QUESTIONS

  Contact {PRIVACY_EMAIL}
"""


# ── Build the ZIP ────────────────────────────────────────────────────────────


def _write_csv_and_json(zf: zipfile.ZipFile, base: str, rows: list[dict]) -> dict:
    """Write base.csv and base.json into the zip. Returns row count info."""
    zf.writestr(f"{base}.csv", _to_csv(rows))
    zf.writestr(f"{base}.json", _to_json(rows))
    return {"rows": len(rows), "files": [f"{base}.csv", f"{base}.json"]}


def build_zip(user_id: int, target_path: Path) -> dict:
    """Render the full export ZIP at *target_path*. Returns the manifest."""
    bundle = _collect(user_id)
    user_email = (bundle["account"] or {}).get("email") or f"user-{user_id}"
    now = int(time.time())
    exported_at_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "schema": "narve.gdpr.export.v1",
        "user_id": user_id,
        "exported_at": exported_at_iso,
        "exported_at_unix": now,
        "files": {},
        "row_counts": {},
        # Schema-drift table/column misses caught by ``_safe_query`` — empty
        # in the happy case, but populated when a deploy drops/renames a
        # table the export references. Surfacing this lets operators (and
        # data subjects) tell a genuinely empty export apart from one that
        # silently skipped tables.
        "errors": list(bundle.get("__errors__") or []),
    }

    with zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # README + account profile + manifest are always present.
        zf.writestr("README.txt", _readme(user_email, exported_at_iso))
        zf.writestr("account.json", _to_json(bundle["account"]))
        manifest["files"]["account.json"] = {"single_row": True}

        zf.writestr("subscriptions.json", _to_json(bundle["subscriptions"]))
        manifest["row_counts"]["subscriptions"] = len(bundle["subscriptions"])

        # Tabular sections — both CSV + JSON for portability.
        for bundle_key, base in (
            ("saved_predictions", "predictions/saved"),
            ("viewed_markets", "markets/viewed"),
            ("followed_sources", "sources/followed"),
            ("market_alerts", "alerts/rules"),
            ("notifications", "notifications/history"),
            ("sessions", "activity/login_history"),
            ("bet_history", "trading/bet_history"),
            ("api_keys", "developer/api_keys"),
            ("telegram_links", "integrations/telegram"),
            ("email_unsubscribes", "notifications/unsubscribes"),
            ("backtests", "trading/backtests"),
            ("feedback", "feedback/submissions"),
            ("gifted_subscriptions", "billing/gifts"),
            # ── New subproduct sections (audit #4 follow-up). ──────────
            ("user_positions", "trading/positions"),
            ("user_predictions", "predictions/user_predictions"),
            ("user_prediction_stats", "predictions/user_prediction_stats"),
            ("user_trading_addon_settings", "trading/addon_settings"),
            ("user_market_credentials", "trading/connections"),
            ("polymarket_connections", "trading/polymarket_connections"),
            ("kalshi_connections", "trading/kalshi_connections"),
            ("whale_watchlist", "whale/watchlist"),
            ("notifications_feed", "notifications/in_app_feed"),
            ("notification_preferences", "notifications/preferences"),
            ("push_subscriptions", "notifications/push_subscriptions"),
            ("engagement_events", "activity/engagement_events"),
            ("user_onboarding", "activity/onboarding"),
            ("user_first_week_goals", "activity/first_week_goals"),
            ("changelog_seen", "activity/changelog_seen"),
            ("market_takes", "social/market_takes"),
            ("take_votes", "social/take_votes"),
            ("user_follows", "social/user_follows"),
            ("collections", "social/collections"),
            ("collection_follows", "social/collection_follows"),
            ("saved_views", "social/saved_views"),
            ("webhook_subscriptions", "developer/webhook_subscriptions"),
            ("feedback_items", "feedback/items"),
            ("feedback_votes", "feedback/votes"),
            ("feedback_comments", "feedback/comments"),
            ("cancellation_attempts", "billing/cancellation_attempts"),
            ("subscription_pauses", "billing/subscription_pauses"),
            ("user_invite_tokens", "social/invite_tokens"),
            ("referrals", "social/referrals"),
            ("affiliate_accounts", "billing/affiliate_accounts"),
            ("weekly_reports", "reports/weekly_reports"),
            ("discord_user_connections", "integrations/discord"),
            ("telegram_connections", "integrations/telegram_connections"),
            ("embed_widgets", "developer/embed_widgets"),
            ("data_export_requests", "activity/data_export_requests"),
            ("audit_log", "activity/audit_log"),
        ):
            rows = bundle.get(bundle_key) or []
            info = _write_csv_and_json(zf, base, rows)
            manifest["files"][base + ".csv"] = info
            manifest["row_counts"][bundle_key] = info["rows"]

        # Signal Search topics — JSON only (the keywords field is a list).
        zf.writestr("signal_search/topics.json", _to_json(bundle["topics"]))
        manifest["row_counts"]["topics"] = len(bundle["topics"])

        # Intelligence conversations — Markdown one file per conversation,
        # plus a JSON index for machine consumers.
        zf.writestr(
            "intelligence/conversations.json",
            _to_json(bundle["conversations"]),
        )
        for conv in bundle["conversations"]:
            conv_id = conv["id"]
            messages = bundle["conversation_messages"].get(conv_id, [])
            md = _conversation_to_markdown(conv, messages)
            zf.writestr(f"intelligence/conversations/conversation-{conv_id}.md", md)
        manifest["row_counts"]["conversations"] = len(bundle["conversations"])
        manifest["row_counts"]["conversation_messages"] = sum(
            len(v) for v in bundle["conversation_messages"].values()
        )

        # Manifest written last so it accurately reflects what we wrote.
        zf.writestr("metadata.json", _to_json(manifest))

    return manifest


# ── Top-level driver (called from the ARQ job) ───────────────────────────────


def generate(export_id: int) -> dict:
    """Generate the ZIP for a queued export request and update the row.

    Returns a status dict suitable for return from the ARQ job.
    """
    import db

    row = db.get_export_request(export_id)
    if row is None:
        return {"export_id": export_id, "status": "missing"}
    user_id = row["user_id"]
    user_row = db.get_user_by_id(user_id) if hasattr(db, "get_user_by_id") else None
    user_email = (user_row["email"] if user_row else None) or f"user-{user_id}"

    # Mark processing.
    db.update_export_status(export_id, status="processing")

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    fname = f"narve-data-export-{user_id}-{ts}.zip"
    target = EXPORT_DIR / fname

    try:
        manifest = build_zip(user_id, target)
        size = target.stat().st_size
        now = int(time.time())
        expires_at = now + EXPORT_TTL_SECONDS
        download_url = sign_download_url(export_id, expires_at)
        db.update_export_status(
            export_id,
            status="ready",
            completed_at=now,
            download_url=download_url,
            expires_at=expires_at,
            file_size_bytes=size,
            file_path=str(target),
        )
    except Exception as e:
        log.exception("export %s failed: %s", export_id, e)
        db.update_export_status(
            export_id,
            status="failed",
            completed_at=int(time.time()),
            error=str(e)[:500],
        )
        return {"export_id": export_id, "status": "failed", "error": str(e)[:200]}

    # Send the "your export is ready" email — fail-soft.
    try:
        from jobs.email_jobs import enqueue_email
        import asyncio

        coro = enqueue_email(
            to=user_email,
            template="data_export_ready",
            context={
                "display_name": (user_email or "").split("@")[0],
                "download_url": download_url,
                "expires_at": expires_at,
                "expires_at_iso": datetime.fromtimestamp(
                    expires_at, tz=timezone.utc
                ).isoformat(),
                "file_size_kb": round(size / 1024, 1),
                "app_url": APP_URL,
            },
            tags=["data_export_ready", "transactional"],
        )
        # If we're inside a running loop (e.g. ARQ worker), just schedule it.
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            asyncio.run(coro)
    except Exception as e:  # pragma: no cover — email is best-effort
        log.warning("export-ready email failed for %s: %s", user_email, e)

    return {
        "export_id": export_id,
        "status": "ready",
        "size_bytes": size,
        "manifest": manifest,
    }
