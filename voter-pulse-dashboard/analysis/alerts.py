"""Mood-move alerts: detect significant moves, compose + send email.

Trigger logic: when the live mood overall changes by more than
ALERT_THRESHOLD_POINTS since the last sent alert (default 5), fan an
email out to every active subscriber. The last-sent mood is stored in
the alert_state table so re-triggers across restarts and per-fetch
caching don't re-send.

Composition: a short HTML + plain-text body containing the old → new
mood, the verbal label transition, a one-paragraph narrative if the AI
banner is available, and an unsubscribe link signed with the same
secret as the rest of the unsubscribe flow.

Transport: plain SMTP via the gateway's existing SMTP_* env vars. Falls
back to logging the rendered email if SMTP isn't configured so dry-runs
work in dev.
"""

from __future__ import annotations

import email.message
import logging
import os
import smtplib
import time
from typing import Optional
from urllib.parse import quote_plus

from ingestion import subscribers

log = logging.getLogger(__name__)

ALERT_KEY_LAST_MOOD     = "last_sent_mood"
ALERT_KEY_LAST_SENT_AT  = "last_sent_at"
DEFAULT_THRESHOLD       = float(os.environ.get("ALERT_THRESHOLD_POINTS", "5"))
MIN_INTERVAL_SECONDS    = int(os.environ.get("ALERT_MIN_INTERVAL_SECONDS", str(6 * 3600)))
FROM_EMAIL              = os.environ.get("ALERT_FROM_EMAIL") or os.environ.get("SMTP_USER") or "alerts@narve.ai"
PUBLIC_URL              = os.environ.get("PUBLIC_DASHBOARD_URL", "https://pulse.narve.ai")


def _read_last_mood() -> float | None:
    raw = subscribers.get_alert_state(ALERT_KEY_LAST_MOOD)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _read_last_sent_at() -> int:
    raw = subscribers.get_alert_state(ALERT_KEY_LAST_SENT_AT)
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _format_label(score: float | None) -> str:
    """Mirror mood_index.label_for so we don't import a cycle."""
    if score is None: return "n/a"
    if score >= 70: return "Good"
    if score >= 55: return "Okay"
    if score >= 40: return "Strained"
    if score >= 25: return "Sour"
    return "Bleak"


def _compose_subject(prior: float | None, current: float) -> str:
    if prior is None:
        return f"Voter Pulse: mood at {current:.0f} ({_format_label(current)})"
    direction = "up" if current > prior else "down"
    return (f"Voter Pulse: mood {direction} from {prior:.0f} to {current:.0f} "
            f"({_format_label(current)})")


def _compose_bodies(prior: float | None, current: float,
                    narrative: str | None, email_addr: str) -> tuple[str, str]:
    token = subscribers.token_for(email_addr)
    unsubscribe_url = (
        f"{PUBLIC_URL.rstrip('/')}/unsubscribe"
        f"?email={quote_plus(email_addr)}&token={quote_plus(token)}"
    )
    delta_line = (
        f"{current:.0f} ({_format_label(current)})" if prior is None
        else f"{prior:.0f} → {current:.0f}  ({_format_label(prior)} → {_format_label(current)})"
    )
    narrative_text = narrative or "(narrative unavailable for this update)"

    text = (
        "Voter Pulse — mood-move alert\n"
        "=================================\n\n"
        f"National mood: {delta_line}\n\n"
        f"{narrative_text}\n\n"
        f"Open the dashboard:  {PUBLIC_URL}\n"
        f"Methodology:         {PUBLIC_URL.rstrip('/')}/methodology\n\n"
        "-- \n"
        "You're receiving this because you subscribed to Voter Pulse mood-move alerts.\n"
        f"Unsubscribe: {unsubscribe_url}\n"
    )
    html = (
        "<!doctype html><html><body style=\"font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "background:#0e1117;color:#e6edf3;padding:24px;margin:0;\">"
        "<div style=\"max-width:560px;margin:0 auto;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:24px;\">"
        "<div style=\"color:#ec4899;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;\">Voter Pulse · narve.ai</div>"
        "<h1 style=\"font-size:18px;margin:8px 0 16px;color:#e6edf3;\">Mood-move alert</h1>"
        f"<div style=\"font-size:32px;font-weight:700;font-variant-numeric:tabular-nums;margin:8px 0;color:#e6edf3;\">{delta_line}</div>"
        f"<p style=\"color:#c9d1d9;line-height:1.55;\">{narrative_text}</p>"
        f"<p><a href=\"{PUBLIC_URL}\" style=\"display:inline-block;padding:10px 18px;background:#ec4899;color:#fff;text-decoration:none;border-radius:4px;font-weight:600;\">Open the dashboard →</a></p>"
        f"<p style=\"color:#8b949e;font-size:11px;margin-top:24px;border-top:1px solid #30363d;padding-top:12px;\">"
        f"You subscribed to Voter Pulse mood-move alerts. <a href=\"{unsubscribe_url}\" style=\"color:#8b949e;\">Unsubscribe</a>."
        "</p></div></body></html>"
    )
    return text, html


def _send_one(to: str, subject: str, text: str, html: str) -> bool:
    """Best-effort SMTP send. Returns True on success, False on any failure
    (logged for the operator). Falls back to logging the message if SMTP
    isn't configured (dev / dry-run)."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        log.info("(dry-run, no SMTP_HOST) → would send to %s: %s", to, subject)
        return True

    msg = email.message.EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pw   = os.environ.get("SMTP_PASS")
    use_ssl = os.environ.get("SMTP_SSL", "0") == "1"

    try:
        cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with cls(host, port, timeout=20) as s:
            if not use_ssl:
                s.starttls()
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as exc:
        log.warning("SMTP send to %s failed: %s", to, exc)
        return False


def check_and_send(current_mood: float | None,
                   narrative_text: str | None,
                   threshold: float | None = None,
                   force: bool = False) -> dict:
    """Decide whether to fire an alert and do so if needed.

    Returns a summary dict the admin endpoint surfaces. Does NOT send if:
      - current_mood is None (no data yet)
      - the mood hasn't moved more than `threshold` since the last alert
      - we sent an alert less than MIN_INTERVAL_SECONDS ago (rate limit)
    Unless `force` is True, in which case it sends anyway provided we have
    a current mood value.
    """
    if current_mood is None:
        return {"sent": 0, "reason": "no current mood"}
    threshold = threshold if threshold is not None else DEFAULT_THRESHOLD

    prior = _read_last_mood()
    last_sent_at = _read_last_sent_at()
    now = int(time.time())

    if not force:
        if prior is not None and abs(current_mood - prior) < threshold:
            return {"sent": 0, "reason": "mood move below threshold",
                    "prior": prior, "current": current_mood, "threshold": threshold}
        if last_sent_at and (now - last_sent_at) < MIN_INTERVAL_SECONDS:
            return {"sent": 0, "reason": "rate-limited",
                    "seconds_since_last": now - last_sent_at,
                    "min_interval_seconds": MIN_INTERVAL_SECONDS}

    recipients = subscribers.list_active()
    if not recipients:
        return {"sent": 0, "reason": "no active subscribers"}

    subject = _compose_subject(prior, current_mood)
    sent = 0
    failures: list[str] = []
    for addr in recipients:
        text, html = _compose_bodies(prior, current_mood, narrative_text, addr)
        if _send_one(addr, subject, text, html):
            sent += 1
        else:
            failures.append(addr)

    if sent > 0:
        subscribers.set_alert_state(ALERT_KEY_LAST_MOOD, f"{current_mood:.2f}")
        subscribers.set_alert_state(ALERT_KEY_LAST_SENT_AT, str(now))

    return {
        "sent": sent,
        "failures": failures,
        "prior": prior,
        "current": current_mood,
        "threshold": threshold,
        "subject": subject,
        "recipients": len(recipients),
    }
