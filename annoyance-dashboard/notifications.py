"""Email notifications for the annoyance dashboard.

Fires one email to each qualifying Pro subscriber every time a new spike
records. "Qualifying" = active Pro/premium/intelligence-addon tier AND
has not unsubscribed AND has not already received this specific spike
AND has not hit the 5-emails-per-24h per-user cap.

Why read ``gateway/auth.db`` directly instead of calling a gateway API:

  * The annoyance-dashboard and the gateway are separate processes on
    the same host. The gateway doesn't currently expose a public
    `/api/users/pro-subscribers` endpoint and adding one would require
    work in another repo.
  * The file is on the same box; the notifier is invoked from a
    background task with no per-request latency budget, so a direct
    read-only sqlite open is fine.
  * Schema-shape changes would break us, but the fields we read
    (``users.email``, ``users.email_marketing``, ``users.suspended``,
    ``users.intelligence_addon_active``, ``subscriptions.plan``,
    ``subscriptions.status``, ``subscriptions.expires_at``) have been
    stable since token-auth shipped. If any go missing we log and
    return zero recipients instead of raising.

Every failure path is fail-soft — a broken SMTP server, a missing
gateway DB, a malformed template — all log and return a summary dict.
The spike detector never blocks on this module.
"""

from __future__ import annotations

import html as _html
import logging
import os
import smtplib
import sqlite3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import config
import db
import url_guard


log = logging.getLogger("annoyance.notifications")

# Safe fallback for any entity_url we were handed that doesn't pass the
# market allowlist. Always on the narve.ai apex so url_guard won't drop it.
_SAFE_FALLBACK_URL = "https://narve.ai/"

TEMPLATE_PATH = Path(__file__).parent / "email_templates" / "spike_alert.html"

# Per-user daily cap — 5 emails / 24h rolling window. Matches spec #7.
MAX_EMAILS_PER_USER_PER_DAY = 5

# Env knobs — all fail-soft.
# Set EMAIL_DRY_RUN=1 in dev to log-and-skip instead of touching SMTP.
GATEWAY_AUTH_DB_ENV = "GATEWAY_AUTH_DB"
SMTP_HOST_ENV = "SMTP_HOST"
SMTP_PORT_ENV = "SMTP_PORT"
SMTP_USER_ENV = "SMTP_USER"
SMTP_PASSWORD_ENV = "SMTP_PASSWORD"
EMAIL_FROM_ENV = "EMAIL_FROM"
EMAIL_FROM_NAME_ENV = "EMAIL_FROM_NAME"
DRY_RUN_ENV = "EMAIL_DRY_RUN"

UNSUBSCRIBE_URL = os.environ.get(
    "EMAIL_UNSUBSCRIBE_URL",
    "https://narve.ai/profile#email-preferences",
).strip()


# ── Template rendering ───────────────────────────────────────────────


def _render_template(
    *,
    entity: str,
    summary: str,
    confidence: float,
    entity_url: str,
    unsubscribe_url: str,
) -> str:
    """Render the spike-alert template with ``str.format_map``.

    No Jinja dependency — the template is a single static file with
    ``{entity}`` / ``{summary}`` / ``{confidence}`` / ``{entity_url}``
    / ``{unsubscribe_url}`` placeholders. All interpolated values are
    HTML-escaped here so the template file can assume safe input.

    If the template is missing or unreadable we fall back to a minimal
    plain-text message so the notification still goes out — strictly
    better than a silent zero-recipients run.
    """
    try:
        raw = TEMPLATE_PATH.read_text()
    except Exception:
        log.exception("notifications: template read failed; using fallback")
        raw = (
            "<html><body><p>Spike detected on "
            "<strong>{entity}</strong> (confidence {confidence}).</p>"
            "<p>{summary}</p>"
            "<p><a href=\"{entity_url}\">View spike</a></p></body></html>"
        )
    try:
        return raw.format_map({
            "entity": _html.escape(entity),
            "summary": _html.escape(summary or "Spike detected — cause pending"),
            "confidence": f"{confidence:.0f}",
            "entity_url": _html.escape(entity_url, quote=True),
            "unsubscribe_url": _html.escape(unsubscribe_url, quote=True),
        })
    except Exception:
        log.exception("notifications: template render failed")
        return raw  # better than nothing


# ── Recipient resolution ─────────────────────────────────────────────


def _gateway_auth_db_path() -> Optional[str]:
    """Resolve the gateway auth.db path, or None if not configured.

    We deliberately don't default to a hard-coded path — the deploy
    script should set ``GATEWAY_AUTH_DB`` explicitly so a misconfigured
    prod run surfaces as "zero recipients" rather than accidentally
    emailing a staging database's users.
    """
    path = os.environ.get(GATEWAY_AUTH_DB_ENV, "").strip()
    if not path:
        return None
    if not Path(path).exists():
        log.warning("notifications: GATEWAY_AUTH_DB=%s does not exist", path)
        return None
    return path


def _resolve_pro_subscribers() -> list[dict]:
    """Read the gateway DB for email-able Pro subscribers.

    The query treats the subscription as active if either (a) the user
    has the intelligence add-on turned on (which grants Pro-equivalent
    privileges across narve.ai), or (b) they have a ``subscriptions``
    row with ``plan IN ('pro', 'premium')`` AND ``status='active'`` AND
    (no expiry OR expiry is in the future).

    Returns an empty list on any error — the caller then logs a
    zero-recipients summary instead of raising.
    """
    path = _gateway_auth_db_path()
    if not path:
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT u.id, u.email
                   FROM users u
                   WHERE u.email_marketing = 1
                     AND u.suspended = 0
                     AND COALESCE(u.is_deleted, 0) = 0
                     AND (
                         u.intelligence_addon_active = 1
                         OR EXISTS (
                             SELECT 1 FROM subscriptions s
                             WHERE s.user_id = u.id
                               AND s.plan IN ('pro', 'premium')
                               AND s.status = 'active'
                               AND (s.expires_at IS NULL
                                    OR s.expires_at > strftime('%s', 'now'))
                         )
                     )"""
            ).fetchall()
            return [{"id": int(r["id"]), "email": r["email"]} for r in rows if r["email"]]
        finally:
            conn.close()
    except Exception:
        log.exception("notifications: gateway DB query failed")
        return []


# ── SMTP send ────────────────────────────────────────────────────────


def _html_to_text(html: str) -> str:
    """Very small HTML→text fallback for the multipart ``plain`` alt.

    Strips tags and collapses whitespace. Email clients use this only
    when they can't render HTML; we don't need anything fancier.
    """
    import re
    txt = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    txt = re.sub(r"</p>", "\n\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _smtp_send(to_email: str, subject: str, html: str) -> tuple[bool, Optional[str]]:
    """Send one email. Returns (success, error_message).

    Honors ``EMAIL_DRY_RUN=1`` — in dry-run we log the intended send
    and return success without touching SMTP, which is how local tests
    + staging deploys exercise the plumbing without risking real email.
    """
    if os.environ.get(DRY_RUN_ENV, "").strip() in ("1", "true", "yes", "on"):
        log.info("notifications: [DRY RUN] to=%s subject=%r", to_email, subject)
        return True, None

    host = os.environ.get(SMTP_HOST_ENV, "").strip()
    if not host:
        return False, "SMTP_HOST not configured"
    try:
        port = int(os.environ.get(SMTP_PORT_ENV, "587"))
    except ValueError:
        port = 587
    smtp_user = os.environ.get(SMTP_USER_ENV, "").strip()
    smtp_pw = os.environ.get(SMTP_PASSWORD_ENV, "")
    sender = os.environ.get(EMAIL_FROM_ENV, "noreply@narve.ai").strip()
    sender_name = os.environ.get(EMAIL_FROM_NAME_ENV, "narve.ai").strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    msg["To"] = to_email
    msg.attach(MIMEText(_html_to_text(html), "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                pass  # server may not support STARTTLS; continue plain
            if smtp_user:
                smtp.login(smtp_user, smtp_pw)
            smtp.sendmail(sender, [to_email], msg.as_string())
        return True, None
    except Exception as exc:
        log.warning("notifications: SMTP send failed to=%s err=%s", to_email, exc)
        return False, str(exc)[:500]


# ── Public entry point ───────────────────────────────────────────────


async def send_spike_email(
    *,
    spike_id: int,
    entity: str,
    summary: str,
    confidence: float,
    entity_url: str,
) -> dict:
    """Notify Pro subscribers about a new spike. Fail-soft throughout.

    Returns a summary dict:
        {"sent": int, "skipped": int, "failed": int, "recipients": int}

    Called from ``spike_detector.detect_and_record`` after a successful
    ``db.insert_spike``. The detector wraps this call in try/except so
    even if we managed to raise (we try not to), the pipeline continues.
    """
    result = {"sent": 0, "skipped": 0, "failed": 0, "recipients": 0}

    # PRE-RELEASE SAFETY: master kill switch. Default OFF — staging ships
    # with this false so spike detection can run without accidentally
    # emailing anyone. See config.py comment for the 3-stage rollout plan.
    if not config.EMAIL_NOTIFICATIONS_ENABLED:
        log.info(
            "notifications: disabled by flag; skipping spike_id=%d entity=%s",
            spike_id, entity,
        )
        return result

    recipients = _resolve_pro_subscribers()

    # Allowlist filter: during soak-test we want the real SMTP/dedup path
    # to fire, but only to a named set of emails (normally our own). Empty
    # allowlist == no filter == fire to everyone.
    if config.EMAIL_NOTIFICATIONS_ALLOWLIST:
        allow = set(config.EMAIL_NOTIFICATIONS_ALLOWLIST)
        before = len(recipients)
        recipients = [r for r in recipients if (r.get("email") or "").lower() in allow]
        log.info(
            "notifications: allowlist filter kept %d of %d recipients (allowlist=%s)",
            len(recipients), before, sorted(allow),
        )

    result["recipients"] = len(recipients)
    if not recipients:
        log.info("notifications: spike_id=%d no Pro recipients", spike_id)
        return result

    # P8.2: entity_url flows straight into an <a href="..."> in the email
    # body. If a curator / upstream caller hands us an off-allowlist URL
    # (bad entity_markets.json entry, mis-built entity path, malicious
    # suggestion that slipped past submission guard), we replace it with a
    # known-safe apex instead of sending users an external link in our
    # brand's name.
    safe_entity_url = entity_url if url_guard.is_allowed_url(entity_url) else _SAFE_FALLBACK_URL
    if safe_entity_url is not entity_url:
        log.warning(
            "notifications: entity_url off allowlist, falling back (spike_id=%d entity=%s)",
            spike_id, entity,
        )

    html = _render_template(
        entity=entity,
        summary=summary,
        confidence=confidence,
        entity_url=safe_entity_url,
        unsubscribe_url=UNSUBSCRIBE_URL,
    )
    subject = f"Annoyance spike: {entity}"

    for r in recipients:
        email = (r.get("email") or "").strip()
        if not email:
            result["skipped"] += 1
            continue

        # Dedup on (spike_id, email) — we've already emailed them this spike.
        try:
            if db.spike_already_emailed(spike_id, email):
                result["skipped"] += 1
                continue
        except Exception:
            log.exception("notifications: dedup check failed")
            # Best-effort: continue rather than skip everyone on DB hiccup.

        # Per-user daily cap (5/24h rolling).
        try:
            if db.count_user_emails_today(email) >= MAX_EMAILS_PER_USER_PER_DAY:
                result["skipped"] += 1
                try:
                    db.record_email_notification(spike_id, email, "skipped", error="daily_cap")
                except Exception:
                    pass
                continue
        except Exception:
            log.exception("notifications: rate limit check failed")

        ok, err = _smtp_send(email, subject, html)
        status = "sent" if ok else "failed"
        try:
            db.record_email_notification(spike_id, email, status, error=err)
        except Exception:
            log.exception("notifications: record_email_notification write failed")
        if ok:
            result["sent"] += 1
        else:
            result["failed"] += 1

    log.info(
        "notifications: spike_id=%d entity=%s sent=%d skipped=%d failed=%d / %d",
        spike_id, entity,
        result["sent"], result["skipped"], result["failed"], result["recipients"],
    )
    return result
