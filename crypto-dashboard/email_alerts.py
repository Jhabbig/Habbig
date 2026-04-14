#!/usr/bin/env python3
"""
Email alert system for CryptoEdge.
Sends email notifications for high-confidence signals via SMTP.
Configure via environment variables or .env file.
"""

import html as _html
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from datetime import datetime, timezone

# SMTP config — set via env vars or .env
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)
FROM_NAME = os.environ.get("FROM_NAME", "CryptoEdge Alerts")


def is_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASS)


def send_alert_email(to_email: str, subject: str, ticker: str, direction: str,
                     confidence: int, delta: str, details: str = ""):
    """Send a signal alert email."""
    if not is_configured():
        return False

    dir_color = "#3fb950" if direction == "positive" else "#f85149"
    dir_label = "BULLISH" if direction == "positive" else "BEARISH"

    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:500px;margin:0 auto;background:#0d1117;color:#e6edf3;border-radius:12px;overflow:hidden;">
      <div style="background:#161b22;padding:20px;border-bottom:1px solid #30363d;">
        <h1 style="margin:0;font-size:1.3em;">CryptoEdge Alert</h1>
        <p style="margin:4px 0 0;color:#8b949e;font-size:0.85em;">{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
      </div>
      <div style="padding:24px;">
        <div style="text-align:center;margin-bottom:20px;">
          <div style="font-size:2em;font-weight:800;color:{dir_color};">{_html.escape(str(ticker))} — {_html.escape(str(dir_label))}</div>
          <div style="font-size:1.2em;color:#8b949e;margin-top:4px;">Confidence: {int(confidence)}%</div>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:0.9em;">
          <tr><td style="padding:8px;color:#8b949e;">Predicted Delta</td><td style="padding:8px;font-weight:700;">{_html.escape(str(delta))}</td></tr>
          <tr><td style="padding:8px;color:#8b949e;">Direction</td><td style="padding:8px;color:{dir_color};font-weight:700;">{dir_label}</td></tr>
          <tr><td style="padding:8px;color:#8b949e;">Confidence</td><td style="padding:8px;font-weight:700;">{confidence}%</td></tr>
        </table>
        {f'<p style="margin-top:16px;color:#8b949e;font-size:0.85em;">{_html.escape(details)}</p>' if details else ''}
        <div style="margin-top:20px;padding:12px;background:rgba(210,153,34,0.1);border:1px solid rgba(210,153,34,0.3);border-radius:8px;font-size:0.75em;color:#d29922;">
          Not financial advice. Predictions are probabilistic. Past accuracy does not guarantee future results.
        </div>
      </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((FROM_NAME, FROM_EMAIL))
    msg["To"] = to_email
    msg.attach(MIMEText(f"{ticker}: {dir_label} signal ({confidence}% confidence). Delta: {delta}", "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[EMAIL] Failed to send to {to_email}: {e}")
        return False


def send_news_trade_alert(to_email: str, alert: dict):
    """Send alert about a news-correlated suspicious trading story."""
    if not is_configured():
        return False

    score = alert.get("score", 0)
    title = alert.get("title") or "Unknown"
    source = alert.get("source") or "Unknown"
    reasons = alert.get("reasons") or []
    related = alert.get("related_markets") or []
    link = alert.get("link") or ""

    reasons_html = "".join(f"<li>{_html.escape(r)}</li>" for r in reasons[:5])
    markets_html = ""
    if related:
        markets_html = "<tr><td style='padding:8px;color:#8b949e;vertical-align:top;'>Related Markets</td><td style='padding:8px;'>"
        for m in related[:3]:
            slug = _html.escape(m.get('slug', ''))
            poly_url = f"https://polymarket.com/event/{slug}"
            markets_html += f'<a href="{_html.escape(poly_url)}" style="color:#58a6ff;text-decoration:none;display:block;margin-bottom:4px;">{_html.escape(m.get("market_question", "")[:60])}</a>'
        markets_html += "</td></tr>"

    subject = f"News-Trade Alert [{score}/100]: {title[:50]}"
    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:500px;margin:0 auto;background:#0d1117;color:#e6edf3;border-radius:12px;overflow:hidden;">
      <div style="background:#161b22;padding:20px;border-bottom:2px solid #d29922;">
        <h1 style="margin:0;font-size:1.3em;color:#d29922;">News-Trade Alert</h1>
        <p style="margin:4px 0 0;color:#8b949e;font-size:0.85em;">Score: {score}/100 &bull; {_html.escape(source)}</p>
      </div>
      <div style="padding:24px;">
        <div style="font-size:1.1em;font-weight:700;margin-bottom:12px;">
          <a href="{_html.escape(link)}" style="color:#e6edf3;text-decoration:none;">{_html.escape(title)}</a>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:0.9em;">
          <tr><td style="padding:8px;color:#8b949e;">Score</td><td style="padding:8px;font-weight:700;color:#d29922;">{score}/100</td></tr>
          <tr><td style="padding:8px;color:#8b949e;">Source</td><td style="padding:8px;">{_html.escape(source)}</td></tr>
          <tr><td style="padding:8px;color:#8b949e;vertical-align:top;">Why flagged</td><td style="padding:8px;"><ul style="margin:0;padding-left:16px;">{reasons_html}</ul></td></tr>
          {markets_html}
        </table>
      </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((FROM_NAME, FROM_EMAIL))
    msg["To"] = to_email
    msg.attach(MIMEText(f"News-Trade Alert [{score}/100]: {title}", "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[EMAIL] Failed to send to {to_email}: {e}")
        return False


def send_suspicious_trade_alert(to_email: str, trade: dict):
    """Send alert about a suspicious trade."""
    if not is_configured():
        return False

    subject = f"Suspicious Trade Alert: ${trade.get('usd_value',0):,.0f} on {trade.get('title','')[:40]}"
    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:500px;margin:0 auto;background:#0d1117;color:#e6edf3;border-radius:12px;overflow:hidden;">
      <div style="background:#161b22;padding:20px;border-bottom:2px solid #f85149;">
        <h1 style="margin:0;font-size:1.3em;color:#f85149;">Suspicious Trade Detected</h1>
      </div>
      <div style="padding:24px;">
        <table style="width:100%;border-collapse:collapse;font-size:0.9em;">
          <tr><td style="padding:8px;color:#8b949e;">Market</td><td style="padding:8px;">{_html.escape(str(trade.get('title','')))}</td></tr>
          <tr><td style="padding:8px;color:#8b949e;">Amount</td><td style="padding:8px;font-weight:700;color:#f85149;">${trade.get('usd_value',0):,.0f}</td></tr>
          <tr><td style="padding:8px;color:#8b949e;">Score</td><td style="padding:8px;font-weight:700;">{trade.get('score',0)}/100</td></tr>
          <tr><td style="padding:8px;color:#8b949e;">Odds</td><td style="padding:8px;">{trade.get('price',0):.0%}</td></tr>
          <tr><td style="padding:8px;color:#8b949e;">Outcome</td><td style="padding:8px;">{_html.escape(str(trade.get('outcome','')))}</td></tr>
        </table>
      </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((FROM_NAME, FROM_EMAIL))
    msg["To"] = to_email
    msg.attach(MIMEText(subject, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[EMAIL] Failed to send to {to_email}: {e}")
        return False
