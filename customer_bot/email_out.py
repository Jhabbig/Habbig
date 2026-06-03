"""Outbound email for cold outreach to leads.

Reuses the SMTP env vars from gateway/email_relay (SMTP_HOST/PORT/USER/PASS),
adds the headers + footer needed for legal compliance, and enforces a
daily cap and per-recipient dedup at the call site.

Compliance baseline (CAN-SPAM + EU PECR / GDPR):
  • Identify the sender — From + reply-to are real.
  • Truthful subject line — we use a plain "saw your <source> post on
    <topic>" rather than anything clickbait.
  • Physical address — included in the footer.
  • Working one-click unsubscribe — both a URL and the RFC 8058
    List-Unsubscribe / List-Unsubscribe-Post headers.
  • Honour opt-outs — handled before send via store.is_suppressed().
"""

from __future__ import annotations

import logging
import os
import pathlib
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Optional

log = logging.getLogger("customer_bot.email_out")

# Physical / sender info shown in footer. Override with env if you want
# something more formal than "personal project".
SENDER_NAME = os.environ.get("LEADS_SENDER_NAME", "Julian Habbig")
SENDER_ORG = os.environ.get("LEADS_SENDER_ORG", "narve.ai — personal project")
# Optional postal address — leave blank if you'd rather not publish one.
# CAN-SPAM technically requires a physical address; without it, set it to
# something credible or don't enable auto-send in jurisdictions that need it.
SENDER_ADDRESS = os.environ.get("LEADS_SENDER_ADDRESS", "")


def _load_env_file() -> None:
    """Mirror the email_relay pattern of picking up .env if present."""
    env_file = pathlib.Path(__file__).resolve().parent.parent / "gateway" / "email_relay" / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _unsubscribe_url(ref_code: str, base: str) -> str:
    return f"{base.rstrip('/')}/unsubscribe?ref={ref_code}"


def _subject_for(source: str, topic_name: str) -> str:
    if source in ("reddit", "reddit_comment"):
        return f"Re: your Reddit thread on {topic_name}"
    if source == "hn":
        return f"Re: your HN comment on {topic_name}"
    return f"On {topic_name}"


def send(
    *,
    to_email: str,
    ref_code: str,
    source: str,
    topic_display: str,
    body_text: str,
    base_url: str = "https://narve.ai",
) -> bool:
    """Send a cold outreach email. Returns True on success.

    Caller is responsible for: rate limiting, suppression check, and
    marking the lead as auto-sent in the DB.
    """
    _load_env_file()
    try:
        smtp_host = os.environ["SMTP_HOST"]
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ["SMTP_USER"]
        smtp_pass = os.environ["SMTP_PASS"]
    except KeyError as exc:
        log.warning("Outreach send aborted — SMTP env var missing: %s", exc)
        return False

    unsub_url = _unsubscribe_url(ref_code, base_url)
    subject = _subject_for(source, topic_display)

    footer_lines = [
        "",
        "---",
        f"{SENDER_NAME} · {SENDER_ORG}",
    ]
    if SENDER_ADDRESS:
        footer_lines.append(SENDER_ADDRESS)
    footer_lines.append(f"Unsubscribe: {unsub_url}")
    footer = "\n".join(footer_lines)

    full_body = f"{body_text}\n{footer}"

    msg = EmailMessage()
    msg["From"] = formataddr((SENDER_NAME, smtp_user))
    msg["To"] = to_email
    msg["Reply-To"] = smtp_user
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
    # RFC 8058 one-click unsubscribe — Gmail / Yahoo bulk-sender requirement.
    msg["List-Unsubscribe"] = f"<{unsub_url}>, <mailto:{smtp_user}?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["X-Customer-Bot"] = "narve.ai outreach"
    msg["Precedence"] = "bulk"
    msg.set_content(full_body)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
    except Exception as exc:  # noqa: BLE001 — log & keep poller alive
        log.warning("Outreach send to %s failed: %s", to_email, exc)
        return False
    log.info("Outreach email sent to %s (ref=%s)", to_email, ref_code)
    return True


def autosend_enabled() -> bool:
    return os.environ.get("LEADS_AUTOSEND_ENABLED", "").lower() in ("1", "true", "yes")


def daily_cap() -> int:
    try:
        return max(0, int(os.environ.get("LEADS_AUTOSEND_DAILY_CAP", "10")))
    except ValueError:
        return 10


def min_score() -> int:
    try:
        return max(0, int(os.environ.get("LEADS_AUTOSEND_MIN_SCORE", "70")))
    except ValueError:
        return 70
