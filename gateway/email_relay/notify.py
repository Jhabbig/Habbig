"""
Bot-side helper to push a notification email to the user.

Usage from any bot:
    from gateway.email_relay.notify import notify
    notify("centralbank", "FOMC arb opened at $0.62 — close edge ~3.2pp",
           details="Polymarket: 0.62  ·  Kalshi implied: 0.65  ·  size: $400")

The user can reply in-thread to drive the bot — the relay matches the
[<bot_key>] subject prefix and pipes the reply body to `claude -p` in that
bot's working directory.
"""
from __future__ import annotations

import os
import pathlib
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

HERE = pathlib.Path(__file__).resolve().parent


def _load_env_file() -> None:
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def notify(bot_key: str, summary: str, details: str = "") -> None:
    """Send a push email '[<bot_key>] <summary>' to NOTIFY_TO."""
    _load_env_file()
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    notify_to = os.environ.get("NOTIFY_TO", smtp_user)

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = notify_to
    msg["Subject"] = f"[{bot_key}] {summary}"
    msg["Message-ID"] = make_msgid(domain=smtp_user.split("@")[-1])
    msg["X-Email-Relay"] = "bot-push"
    msg["X-Bot-Key"] = bot_key
    msg.set_content(f"{summary}\n\n{details}".strip() if details else summary)

    with smtplib.SMTP(smtp_host, smtp_port) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        sys.exit("usage: python3 -m gateway.email_relay.notify <bot_key> <summary> [details]")
    notify(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
