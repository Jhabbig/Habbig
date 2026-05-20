from __future__ import annotations
"""Notification delivery: email (SMTP) + web push (VAPID).

All transports are best-effort and fail-soft — a notification that can't be
delivered is logged but never crashes the alert worker. Credentials come from
env vars; if a transport's credentials aren't set, that channel is skipped
silently.

Env vars:
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_USE_TLS
    VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, VAPID_SUBJECT
"""

import asyncio
import json
import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)


def _smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST")) and bool(os.getenv("SMTP_FROM"))


def _vapid_configured() -> bool:
    return bool(os.getenv("VAPID_PRIVATE_KEY")) and bool(os.getenv("VAPID_SUBJECT"))


async def send_email(to_email: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send an email via SMTP. Returns True on success, False otherwise.

    Runs the blocking smtplib call in a thread executor so it doesn't block
    the event loop. Silent no-op when SMTP isn't configured (returns False).
    """
    if not _smtp_configured():
        return False
    if not to_email:
        return False

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM")
    use_tls = os.getenv("SMTP_USE_TLS", "1") != "0"

    def _send() -> bool:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = to_email
            if text:
                msg.attach(MIMEText(text, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))

            if use_tls:
                context = ssl.create_default_context()
                with smtplib.SMTP(host, port, timeout=10) as server:
                    server.starttls(context=context)
                    if user:
                        server.login(user, password)
                    server.sendmail(sender, [to_email], msg.as_string())
            else:
                with smtplib.SMTP(host, port, timeout=10) as server:
                    if user:
                        server.login(user, password)
                    server.sendmail(sender, [to_email], msg.as_string())
            return True
        except Exception as e:
            logger.warning(f"SMTP send to {to_email} failed: {e}")
            return False

    return await asyncio.get_event_loop().run_in_executor(None, _send)


async def send_web_push(subscription: dict, payload: dict) -> bool:
    """Send a web push notification to a single subscription.

    *subscription* is the dict returned by the browser's
    ``PushManager.subscribe()`` (endpoint + keys). *payload* is a JSON-able
    dict that the service worker will read.
    """
    if not _vapid_configured():
        return False
    try:
        from pywebpush import webpush, WebPushException  # type: ignore
    except ImportError:
        logger.warning("pywebpush not installed; skipping push delivery")
        return False

    def _send() -> bool:
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(payload),
                vapid_private_key=os.getenv("VAPID_PRIVATE_KEY"),
                vapid_claims={"sub": os.getenv("VAPID_SUBJECT", "mailto:noreply@example.com")},
            )
            return True
        except WebPushException as e:
            logger.warning(f"Web push failed: {e}")
            return False
        except Exception as e:
            logger.warning(f"Web push error: {e}")
            return False

    return await asyncio.get_event_loop().run_in_executor(None, _send)


def vapid_public_key() -> Optional[str]:
    """The base64url-encoded public key the frontend uses to subscribe."""
    pk = os.getenv("VAPID_PUBLIC_KEY", "").strip()
    return pk or None


def channels_available() -> dict:
    """Which notification channels are configured."""
    return {
        "email": _smtp_configured(),
        "push": _vapid_configured(),
    }
