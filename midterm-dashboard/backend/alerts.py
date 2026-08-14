"""Alert dispatch for the midterm dashboard.

Triggered when a watched race's cross-source max-divergence crosses a
user-configured threshold. Sends email (SMTP) and / or Telegram notifications,
plus an in-app entry in ``midterm_alert_history``.

Configuration is by environment variables — all optional. If a channel isn't
configured the dispatcher silently skips it.

  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / FROM_EMAIL / FROM_NAME
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

The crypto-dashboard ships a similar email module — we don't share a single
implementation across dashboards because each ships in a separate Docker
image, but the structure is intentionally compatible.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional

import aiohttp

logger = logging.getLogger("midterm.alerts")

# ── SMTP ──────────────────────────────────────────────────────────────
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)
FROM_NAME = os.environ.get("FROM_NAME", "narve.ai Midterm Alerts")

# ── Telegram ──────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")  # default chat (optional)


def email_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASS)


def telegram_configured() -> bool:
    return bool(TG_TOKEN)


def send_email(to_email: str, subject: str, html: str, plain: str) -> bool:
    """Synchronous SMTP send. Returns True on success."""
    if not email_configured():
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((FROM_NAME, FROM_EMAIL))
        msg["To"] = to_email
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        logger.warning(f"SMTP send failed to {to_email}: {e}")
        return False


async def send_telegram(text: str, chat_id: Optional[str] = None) -> bool:
    """Async Telegram message via Bot API. Returns True on success."""
    if not telegram_configured():
        return False
    chat = chat_id or TG_CHAT
    if not chat:
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={
                "chat_id": chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Telegram send {resp.status}: {body[:200]}")
                    return False
                return True
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False


def build_divergence_html(race_key: str, threshold: float, max_div: float, sources: dict) -> tuple[str, str]:
    """Return (html, plain) bodies for a divergence alert."""
    rows = "".join(
        f"<tr><td style='padding:6px 12px;color:#8b949e;'>{_html.escape(src)}</td>"
        f"<td style='padding:6px 12px;font-weight:700;'>{(prob * 100):.1f}%</td></tr>"
        for src, prob in sources.items()
        if prob is not None
    )
    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:520px;margin:0 auto;background:#0d1117;color:#e6edf3;border-radius:12px;overflow:hidden;">
      <div style="background:#161b22;padding:20px;border-bottom:1px solid #30363d;">
        <h1 style="margin:0;font-size:1.3em;">Divergence Alert</h1>
        <p style="margin:4px 0 0;color:#8b949e;font-size:0.85em;">{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
      </div>
      <div style="padding:24px;">
        <div style="font-size:1.1em;margin-bottom:16px;">
          <strong>{_html.escape(race_key)}</strong> divergence is
          <span style="color:#f85149;font-weight:700;">{max_div * 100:.1f}pp</span>
          (threshold: {threshold:.1f}pp)
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:0.95em;">
          {rows}
        </table>
        <a href="https://midterm.narve.ai/race/{_html.escape(race_key)}"
           style="display:inline-block;margin-top:16px;padding:10px 16px;background:#2f81f7;color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">
          View race
        </a>
      </div>
    </div>
    """
    plain = (
        f"Divergence alert: {race_key} is {max_div * 100:.1f}pp (threshold {threshold:.1f}pp). "
        + "Sources: "
        + ", ".join(f"{s}={p * 100:.1f}%" for s, p in sources.items() if p is not None)
        + f" — https://midterm.narve.ai/race/{race_key}"
    )
    return html, plain


async def dispatch_divergence_alert(
    *,
    user_email: Optional[str],
    user_telegram_chat: Optional[str],
    race_key: str,
    threshold: float,
    max_div: float,
    sources: dict,
) -> dict:
    """Send a divergence alert through whatever channels are configured.

    Returns a dict ``{"email": bool, "telegram": bool}`` indicating which
    channels succeeded.
    """
    html, plain = build_divergence_html(race_key, threshold, max_div, sources)
    subject = f"narve.ai · {race_key} divergence {max_div * 100:.1f}pp"

    email_ok = False
    telegram_ok = False

    if user_email and email_configured():
        # SMTP is sync — run in a thread so we don't block the event loop.
        email_ok = await asyncio.to_thread(send_email, user_email, subject, html, plain)

    if user_telegram_chat and telegram_configured():
        telegram_ok = await send_telegram(plain.replace("https://", "\n"), chat_id=user_telegram_chat)

    return {"email": email_ok, "telegram": telegram_ok}
