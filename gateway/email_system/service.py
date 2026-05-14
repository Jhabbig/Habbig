"""EmailService — the single chokepoint for outbound mail.

Transport precedence:

  1. EMAIL_DRY_RUN=true        → log the email, do not send
  2. EMAIL_RELAY_URL set       → POST to MailChannels Cloudflare Worker relay
  3. SMTP_HOST set             → plain SMTP (aiosmtplib or sync fallback)
  4. Nothing set               → log a warning, return False

Never raises — logs and returns False on failure so callers (ARQ jobs)
can decide whether to retry. The job queue handles retry logic.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

import httpx

from email_system.renderer import render, render_text_fallback


log = logging.getLogger("email")


class EmailService:
    def __init__(self) -> None:
        self.from_address = os.environ.get("EMAIL_FROM", "noreply@narve.ai")
        self.from_name = os.environ.get("EMAIL_FROM_NAME", "narve.ai")
        self.smtp_host = os.environ.get("SMTP_HOST", "").strip()
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.smtp_user = os.environ.get("SMTP_USER", "").strip()
        self.smtp_password = os.environ.get("SMTP_PASSWORD", "").strip()
        self.relay_url = os.environ.get("EMAIL_RELAY_URL", "").strip()
        self.relay_secret = os.environ.get("EMAIL_RELAY_SECRET", "").strip()
        self.dry_run = os.environ.get("EMAIL_DRY_RUN", "false").lower() == "true"
        self.app_url = os.environ.get("APP_URL", "https://narve.ai")

    async def send(
        self,
        to: str,
        subject: str,
        html: str,
        text: Optional[str] = None,
        reply_to: Optional[str] = None,
        tags: Optional[list] = None,
    ) -> bool:
        """Send a pre-rendered email. Returns True on success, False on failure."""
        if text is None:
            text = render_text_fallback(html)

        if self.dry_run:
            log.info(
                "EMAIL DRY RUN to=%s subject=%r tags=%s html_len=%d",
                to, subject, tags or [], len(html),
            )
            return True

        if self.relay_url:
            return await self._send_via_relay(to, subject, html, text, reply_to, tags)

        if self.smtp_host:
            return self._send_via_smtp(to, subject, html, text, reply_to)

        log.warning(
            "EMAIL NOT SENT — no transport configured (set EMAIL_DRY_RUN, EMAIL_RELAY_URL, or SMTP_HOST). to=%s",
            to,
        )
        return False

    async def send_template(
        self,
        to: str,
        template: str,
        context: dict,
        reply_to: Optional[str] = None,
        tags: Optional[list] = None,
    ) -> bool:
        """Render a named template and send.

        `context` is passed verbatim plus `app_url` which every template
        has access to for link building. `subject` is read from the
        rendered template's <title> tag (or a SUBJECTS mapping fallback).
        """
        ctx = dict(context)
        ctx.setdefault("app_url", self.app_url)

        # Welcome template has three mutually-exclusive variants
        # (pro / subproduct / generic). If the caller didn't pick one,
        # fall back to generic so existing call sites and admin overrides
        # that pre-date subproduct-awareness still render a body.
        if template == "welcome":
            if not (ctx.get("is_pro_welcome") or ctx.get("subproduct_name")):
                ctx.setdefault("is_generic_welcome", True)

        override_subject, override_html = _resolve_admin_override(template, ctx)
        if override_html is not None:
            return await self.send(
                to=to,
                subject=override_subject or _SUBJECTS.get(template, "narve.ai"),
                html=override_html,
                text=render_text_fallback(override_html),
                reply_to=reply_to,
                tags=tags,
            )

        try:
            html = render(template, ctx)
        except Exception as e:
            log.exception("email template render failed: %s — %s", template, e)
            return False

        subject = _SUBJECTS.get(template, "narve.ai")
        # Allow child templates to override via {{ subject }} in context.
        if "subject" in ctx:
            subject = ctx["subject"]

        return await self.send(
            to=to,
            subject=subject,
            html=html,
            text=render_text_fallback(html),
            reply_to=reply_to,
            tags=tags,
        )

    # ── transports ─────────────────────────────────────────────────────

    async def _send_via_relay(
        self, to: str, subject: str, html: str, text: str,
        reply_to: Optional[str], tags: Optional[list],
    ) -> bool:
        """POST to the MailChannels Cloudflare Worker relay (see CLOUDFLARE_CHANGES.md)."""
        headers = {"Content-Type": "application/json"}
        if self.relay_secret:
            headers["Authorization"] = f"Bearer {self.relay_secret}"
        body = {
            "to": to,
            "from": self.from_address,
            "fromName": self.from_name,
            "subject": subject,
            "html": html,
            "text": text,
            "replyTo": reply_to,
            "tags": tags or [],
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.post(self.relay_url, json=body, headers=headers)
            if 200 <= resp.status_code < 300:
                log.info("email sent via relay to=%s template_subject=%r", to, subject)
                return True
            log.warning("relay returned %d: %s", resp.status_code, resp.text[:200])
            return False
        except Exception as e:
            log.warning("relay send failed: %s", e)
            return False

    def _send_via_smtp(
        self, to: str, subject: str, html: str, text: str, reply_to: Optional[str]
    ) -> bool:
        """Plain SMTP fallback. Blocking — runs fine from an async job because
        ARQ / the in-process backend already parallelise jobs."""
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{self.from_name} <{self.from_address}>"
        msg["To"] = to
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.starttls(context=context)
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)
            log.info("email sent via smtp to=%s subject=%r", to, subject)
            return True
        except Exception as e:
            log.warning("smtp send failed: %s", e)
            return False


_SUBJECTS = {
    "token_delivery": "Your narve.ai access token",
    "welcome": "Welcome to narve.ai",
    "payment_failed": "Payment failed — action required",
    "subscription_cancelled": "Your narve.ai subscription has ended",
    "password_reset": "Reset your narve.ai password",
    "account_deletion_confirmation": "Account deletion requested — narve.ai",
    "account_deleted": "Your narve.ai account has been deleted",
    "weekly_digest": "narve.ai — Your weekly signal digest",
    "market_resolved": "Market resolved on narve.ai",
    "unsubscribe_confirmation": "Unsubscribed from narve.ai emails",
    "enquiry_notification": "New enquiry — narve.ai",
    "newsletter_confirm": "Confirm your narve.ai subscription",
    "2fa_email_otp": "Your narve.ai sign-in code",
    "2fa_locked": "Suspicious activity on your narve.ai account",
    "incident_created": "New incident — narve.ai status",
    "incident_update": "Incident update — narve.ai status",
    "incident_resolved": "Incident resolved — narve.ai status",
    "webhook_disabled": "Webhook disabled — narve.ai",
    "winback_7d": "Your narve.ai dashboard is waiting",
    "winback_30d": "Your seat at narve.ai is still here",
    "saved_prediction_resolved": "Your saved prediction just resolved",
    "weekly_intelligence": "narve.ai — Your weekly intelligence report",
    "admin_cost_alert": "[admin] narve.ai Claude spend alert",
    "admin_subscription_drift": "[admin] narve.ai subscription drift detected",
}



def _substitute(text, ctx):
    """Minimal `{{ key }}` / `{{ raw_key }}` substitution for admin-edited templates.

    Mirrors render_page: raw_-prefixed keys verbatim, all others HTML-escaped.
    Keeps admin templates predictable without pulling Jinja/Mustache in.
    """
    import html as _html
    import re as _re

    def repl(m):
        key = m.group(1).strip()
        raw = key.startswith("raw_")
        if key not in ctx:
            return ""
        value = ctx.get(key)
        if value is None:
            return ""
        value = str(value)
        return value if raw else _html.escape(value)

    return _re.sub(r"\{\{\s*([\w\.]+)\s*\}\}", repl, text)


def _resolve_admin_override(template, ctx):
    """Try to load+render an admin override. Returns (subject, html) or (None, None).

    A broken override (render error, empty body) falls through so the caller
    uses the file template — we never drop an email because of bad admin HTML.
    """
    try:
        import db
        row = db.get_email_template(template)
    except Exception as exc:
        log.warning("email template lookup failed %s: %s", template, exc)
        return None, None

    if not row or not row["is_active"]:
        return None, None

    try:
        subject = _substitute(row["subject"] or "", ctx)
        body = _substitute(row["body_html"] or "", ctx)
        if not body.strip():
            return None, None
        return subject, body
    except Exception as exc:
        log.warning("admin email template render failed %s: %s", template, exc)
        return None, None


def render_preview(subject, body_html, variables, sample_overrides=None):
    """Render a template preview with sample data for missing vars.

    Never raises — the admin editor fetches this endpoint on every keystroke
    so an exception would make the preview panel go blank in weird ways.
    """
    defaults = {v: f"Sample {v}" for v in (variables or [])}
    defaults.update(sample_overrides or {})
    defaults.setdefault("app_url", "https://narve.ai")
    try:
        return {
            "subject": _substitute(subject or "", defaults),
            "html": _substitute(body_html or "", defaults),
        }
    except Exception as exc:
        return {"subject": f"[preview error: {exc}]", "html": ""}
