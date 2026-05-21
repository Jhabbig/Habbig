"""SMTP sender with DRY_RUN fallback — v1.6.

If SMTP_HOST is unset, the sender logs the message and returns success.
That lets the sandbox + DEV_MODE exercise the full digest flow (subscribe
→ confirm → dispatch) without external SMTP. Set SMTP_HOST in production
to actually deliver mail.

Env vars:
    SMTP_HOST       — server hostname; unset = DRY_RUN
    SMTP_PORT       — default 587 (STARTTLS) or 465 (implicit TLS)
    SMTP_USER       — auth user (optional; some relays are IP-allowlisted)
    SMTP_PASS       — auth password
    SMTP_FROM       — From: header (default 'noreply@regulators.local')
    SMTP_STARTTLS   — '1' (default) to upgrade with STARTTLS on port 587

This is the lightweight stdlib path. For deliverability at scale (DKIM,
SPF, dedicated IP, bounce processing) a managed provider (Postmark,
SendGrid, AWS SES) is the right next layer — drop a one-file replacement
of this module behind the same `send()` signature.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def is_dry_run() -> bool:
    return not _env("SMTP_HOST")


def send(*, to_addr: str, subject: str, html_body: str, text_body: str) -> dict:
    """Send a multipart email. DRY_RUN mode logs and returns
    `{ok: True, dry_run: True, ...}` without contacting any server."""
    if not to_addr or "@" not in to_addr:
        return {"ok": False, "error": "invalid recipient"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _env("SMTP_FROM", "noreply@regulators.local")
    msg["To"] = to_addr
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if is_dry_run():
        log.info(
            "[email DRY_RUN] to=%s subj=%r text_len=%d html_len=%d",
            to_addr, subject, len(text_body), len(html_body),
        )
        return {"ok": True, "dry_run": True, "to": to_addr, "subject": subject}

    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT", "587"))
    user = _env("SMTP_USER")
    password = _env("SMTP_PASS")
    starttls = _env("SMTP_STARTTLS", "1") == "1"

    try:
        if port == 465:
            srv = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            srv = smtplib.SMTP(host, port, timeout=30)
            if starttls:
                srv.starttls()
        if user:
            srv.login(user, password)
        srv.send_message(msg)
        srv.quit()
        return {"ok": True, "dry_run": False, "to": to_addr, "subject": subject}
    except Exception as exc:
        log.warning("SMTP send failed to %s: %s", to_addr, exc)
        return {"ok": False, "dry_run": False, "to": to_addr, "error": str(exc)}
