#!/usr/bin/env python3
"""
Daily email digest of insider activity.

Replaces the per-event SMTP push from `watchlist._dispatch_external` (which
produced one email per filed PTR — spammy). The new model:

  - Once a day at DIGEST_HOUR (default 7am local), a poller composes a
    single email per user covering the prior 24h:
      • All new events for actors on their watchlist
      • Top cross-venue |Δ_pre| moves across all venues (top 10)
      • Newest watched-actor inbox alerts not yet read
  - Per-event SMTP can still be enabled with ALERT_MODE=per_event.
    Default is ALERT_MODE=digest (silent SMTP for individual events).

Configuration (env vars, all optional — degrades to in-app inbox only):
  ALERTS_SMTP_HOST, ALERTS_SMTP_PORT (default 587),
  ALERTS_SMTP_USER, ALERTS_SMTP_PASS, ALERTS_FROM_ADDR
  ALERTS_USER_<USERID> = recipient@example.com
  DIGEST_HOUR_LOCAL = 7   # 24h clock; default 7am
  DIGEST_TZ = UTC          # IANA name; default UTC

Idempotency: each digest send writes to a `digest_sends` table; the
poller checks "did we already send a digest for user U on date D?"
before composing a new one. Manual `send_now(user_id)` ignores that
check.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "watchlist.db"  # share with watchlist
EVENTS_DB_PATH = Path(__file__).parent / "insider_events.db"

# Default mode: 'digest' (one email/day), 'per_event' (every event), 'both'
ALERT_MODE = os.environ.get("ALERT_MODE", "digest").strip().lower()
DIGEST_HOUR_LOCAL = int(os.environ.get("DIGEST_HOUR_LOCAL", "7"))
DIGEST_TZ_NAME = os.environ.get("DIGEST_TZ", "UTC")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS digest_sends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    send_date   TEXT NOT NULL,           -- YYYY-MM-DD in local tz
    sent_at     INTEGER NOT NULL,
    item_count  INTEGER NOT NULL DEFAULT 0,
    smtp_ok     INTEGER NOT NULL DEFAULT 0,  -- 1 if SMTP reported success
    error       TEXT,
    UNIQUE(user_id, send_date)
);
CREATE INDEX IF NOT EXISTS idx_digest_user_date
    ON digest_sends(user_id, send_date DESC);
"""


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


# ─── SMTP helpers (mirror watchlist._dispatch_*) ──────────────────────

def _smtp_available() -> bool:
    return all(os.environ.get(k) for k in (
        "ALERTS_SMTP_HOST", "ALERTS_SMTP_USER",
        "ALERTS_SMTP_PASS", "ALERTS_FROM_ADDR",
    ))


def _user_email(user_id: str) -> str | None:
    safe = (user_id or "").replace("-", "_").replace(".", "_")
    return os.environ.get(f"ALERTS_USER_{safe}")


def _send(to_addr: str, subject: str, plain: str, html: str | None = None) -> tuple[bool, str | None]:
    if not _smtp_available():
        return False, "smtp_unavailable"
    msg = EmailMessage()
    msg["From"] = os.environ["ALERTS_FROM_ADDR"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(plain)
    if html:
        msg.add_alternative(html, subtype="html")

    host = os.environ["ALERTS_SMTP_HOST"]
    port = int(os.environ.get("ALERTS_SMTP_PORT", "587"))
    user = os.environ["ALERTS_SMTP_USER"]
    pwd = os.environ["ALERTS_SMTP_PASS"]
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        return True, None
    except Exception as e:
        return False, str(e)


# ─── Digest content gathering ─────────────────────────────────────────

def _watched_actor_ids(user_id: str) -> dict[str, str]:
    """{actor_id: label} for the user's watchlist. Local read."""
    with _conn() as c:
        rows = c.execute(
            "SELECT actor_id, label FROM watchlist WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {r["actor_id"]: (r["label"] or r["actor_id"]) for r in rows}


def _new_events_for_watched(actor_ids: list[str], since_ts: int) -> list[dict]:
    """All events from watched actors filed in the last 24h, joined for context."""
    if not actor_ids:
        return []
    placeholders = ",".join("?" for _ in actor_ids)
    sql = f"""
        SELECT id, venue, actor_id, actor_label, actor_role,
               symbol, symbol_name, side,
               size_usd_low, size_usd_high,
               ts_filed, ts_executed, raw_url
        FROM insider_events
        WHERE actor_id IN ({placeholders})
          AND COALESCE(ts_filed, ts_executed, created_at) >= ?
        ORDER BY COALESCE(ts_filed, ts_executed, created_at) DESC
    """
    with sqlite3.connect(EVENTS_DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(sql, [*actor_ids, since_ts]).fetchall()
    return [dict(r) for r in rows]


def _top_cross_venue_moves(since_ts: int, *, limit: int = 10) -> list[dict]:
    """Recent biggest |Δ_pre| correlation rows for the digest's "across all venues"
    section. Joins back to insider_events for the actor name."""
    sql = """
        SELECT
            c.event_id, c.market_id, c.market_question,
            c.ticker, c.ts_disclosure, c.delta_pre, c.delta_post,
            c.price_at_disclosure,
            e.venue, e.actor_id, e.actor_label, e.side,
            e.size_usd_low, e.size_usd_high
        FROM insider_market_correlations c
        JOIN insider_events e ON e.id = c.event_id
        WHERE c.delta_pre IS NOT NULL
          AND c.ts_disclosure >= ?
        ORDER BY ABS(c.delta_pre) DESC
        LIMIT ?
    """
    with sqlite3.connect(EVENTS_DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(sql, (since_ts, limit)).fetchall()
    return [dict(r) for r in rows]


def _unread_inbox(user_id: str, *, limit: int = 20) -> list[dict]:
    """Mirror of watchlist.list_inbox(unread_only=True) with cross-DB attach."""
    sql = """
        SELECT i.id AS inbox_id, i.event_id, i.created_at AS alerted_at,
               e.venue, e.symbol, e.symbol_name, e.side,
               e.size_usd_low, e.size_usd_high,
               e.actor_label, e.raw_url, e.ts_filed
        FROM alert_inbox i
        JOIN ev.insider_events e ON e.id = i.event_id
        WHERE i.user_id = ? AND i.read_at IS NULL
        ORDER BY i.created_at DESC LIMIT ?
    """
    with _conn() as c:
        c.execute("ATTACH DATABASE ? AS ev", (str(EVENTS_DB_PATH),))
        try:
            rows = c.execute(sql, (user_id, limit)).fetchall()
        finally:
            c.execute("DETACH DATABASE ev")
    return [dict(r) for r in rows]


# ─── Formatting ──────────────────────────────────────────────────────

def _fmt_size(low: float | None, high: float | None) -> str:
    if not low and not high:
        return "—"
    if low and high and low != high:
        return f"${low:,.0f} – ${high:,.0f}"
    val = high or low
    return f"${val:,.0f}"


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "—"


def build_digest_content(user_id: str, *, since_hours: int = 24) -> dict:
    """
    Returns {plain, html, item_count, sections: {watched, top_moves, inbox}}.
    item_count = total interesting things to report; if 0 the caller should skip.
    """
    init_db()
    since_ts = int(time.time()) - since_hours * 3600
    watches = _watched_actor_ids(user_id)
    watched_events = _new_events_for_watched(list(watches.keys()), since_ts) if watches else []
    top_moves = _top_cross_venue_moves(since_ts, limit=10)
    inbox = _unread_inbox(user_id, limit=20)

    item_count = len(watched_events) + len(top_moves) + len(inbox)

    # Plain text — keep tight, scannable
    lines = [
        f"INSIDER DASHBOARD DIGEST — last {since_hours}h",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    if watches:
        lines.append(f"You're watching {len(watches)} actor(s).")
    lines.append("")

    if watched_events:
        lines.append(f"━━ WATCHED ACTOR ACTIVITY ({len(watched_events)}) ━━")
        for e in watched_events[:30]:
            lines.append(
                f"  {_fmt_ts(e['ts_filed'])} · {e.get('actor_label')} · "
                f"{(e.get('side') or '?').upper()} {e.get('symbol') or '—'} "
                f"({_fmt_size(e.get('size_usd_low'), e.get('size_usd_high'))})"
            )
            if e.get("raw_url"):
                lines.append(f"      {e['raw_url']}")
        lines.append("")
    else:
        lines.append("━━ WATCHED ACTOR ACTIVITY ━━")
        lines.append("  (no new filings from your watchlist in the last 24h)")
        lines.append("")

    if top_moves:
        lines.append(f"━━ TOP CROSS-VENUE MOVES ({len(top_moves)}) ━━")
        lines.append("  Biggest |Δ_pre| in the 24h before disclosure")
        lines.append("")
        for c in top_moves:
            sign = "+" if (c.get("delta_pre") or 0) >= 0 else ""
            lines.append(
                f"  {_fmt_ts(c['ts_disclosure'])} · {c.get('actor_label') or c.get('actor_id')} · "
                f"{c.get('ticker')} · Δ_pre={sign}{(c.get('delta_pre') or 0):.3f} "
                f"(price={c.get('price_at_disclosure') or 0:.3f})"
            )
            if c.get("market_question"):
                lines.append(f"      market: {c['market_question'][:90]}")
        lines.append("")

    if inbox:
        lines.append(f"━━ UNREAD ALERTS ({len(inbox)}) ━━")
        for i in inbox[:15]:
            lines.append(
                f"  {_fmt_ts(i['alerted_at'])} · {i.get('actor_label')} · "
                f"{(i.get('side') or '?').upper()} {i.get('symbol') or '—'}"
            )
        lines.append("")

    lines.append("---")
    lines.append("Manage your watchlist + inbox at https://traders.narve.ai/")
    lines.append("Reply STOP to disable digests (or unset ALERTS_USER_<id>).")

    plain = "\n".join(lines)

    # Minimal HTML version — readable in any client, no inline CSS deps
    html_parts = [
        "<html><body style='font-family: -apple-system, sans-serif; font-size: 14px; color: #222; max-width: 720px;'>",
        "<h2 style='color:#b8860b; margin-bottom: 4px;'>Insider Dashboard Digest</h2>",
        f"<div style='color:#666; font-size:12px;'>last {since_hours}h · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · watching {len(watches)} actor(s)</div>",
        "<hr/>",
    ]

    def _row(rows: list[str]) -> str:
        return "<table style='width:100%; border-collapse:collapse;'>" + "".join(rows) + "</table>"

    if watched_events:
        html_parts.append(f"<h3>Watched actor activity ({len(watched_events)})</h3>")
        rows = []
        for e in watched_events[:30]:
            url = e.get("raw_url") or "#"
            rows.append(
                f"<tr style='border-bottom:1px solid #eee;'>"
                f"<td style='padding:6px 8px;'>{_fmt_ts(e['ts_filed'])}</td>"
                f"<td style='padding:6px 8px;'><strong>{(e.get('actor_label') or '')[:40]}</strong></td>"
                f"<td style='padding:6px 8px;'>{(e.get('side') or '?').upper()}</td>"
                f"<td style='padding:6px 8px;'><a href='{url}'>{e.get('symbol') or '—'}</a></td>"
                f"<td style='padding:6px 8px; color:#555;'>{_fmt_size(e.get('size_usd_low'), e.get('size_usd_high'))}</td>"
                f"</tr>"
            )
        html_parts.append(_row(rows))

    if top_moves:
        html_parts.append(f"<h3>Top cross-venue moves ({len(top_moves)})</h3>")
        html_parts.append("<div style='color:#666; font-size:12px;'>biggest |Δ_pre| in the 24h before disclosure</div>")
        rows = []
        for c in top_moves:
            d = c.get("delta_pre") or 0
            color = "#0a7" if d >= 0 else "#a04"
            rows.append(
                f"<tr style='border-bottom:1px solid #eee;'>"
                f"<td style='padding:6px 8px;'>{_fmt_ts(c['ts_disclosure'])}</td>"
                f"<td style='padding:6px 8px;'>{(c.get('actor_label') or c.get('actor_id') or '')[:35]}</td>"
                f"<td style='padding:6px 8px;'><strong>{c.get('ticker')}</strong></td>"
                f"<td style='padding:6px 8px; color:{color}; font-weight:bold;'>"
                f"{'+' if d >= 0 else ''}{d:.3f}</td>"
                f"<td style='padding:6px 8px; color:#555; font-size:12px;'>"
                f"{(c.get('market_question') or '')[:60]}</td>"
                f"</tr>"
            )
        html_parts.append(_row(rows))

    if inbox:
        html_parts.append(f"<h3>Unread alerts ({len(inbox)})</h3>")
        rows = []
        for i in inbox[:15]:
            rows.append(
                f"<tr style='border-bottom:1px solid #eee;'>"
                f"<td style='padding:6px 8px;'>{_fmt_ts(i['alerted_at'])}</td>"
                f"<td style='padding:6px 8px;'>{(i.get('actor_label') or '')[:30]}</td>"
                f"<td style='padding:6px 8px;'>{(i.get('side') or '?').upper()} {i.get('symbol') or '—'}</td>"
                f"</tr>"
            )
        html_parts.append(_row(rows))

    html_parts.append("<hr/>")
    html_parts.append("<div style='color:#666; font-size:12px;'>"
                      "Manage at <a href='https://traders.narve.ai/'>traders.narve.ai</a></div>")
    html_parts.append("</body></html>")

    return {
        "plain": plain,
        "html": "\n".join(html_parts),
        "item_count": item_count,
        "sections": {
            "watched_events": len(watched_events),
            "top_moves": len(top_moves),
            "unread_inbox": len(inbox),
        },
    }


# ─── Send ────────────────────────────────────────────────────────────

def _record_send(user_id: str, send_date: str, item_count: int,
                 smtp_ok: bool, error: str | None) -> None:
    init_db()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO digest_sends "
            "(user_id, send_date, sent_at, item_count, smtp_ok, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, send_date, int(time.time()), item_count,
             1 if smtp_ok else 0, error),
        )


def _already_sent_today(user_id: str, send_date: str) -> bool:
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM digest_sends WHERE user_id = ? AND send_date = ? AND smtp_ok = 1",
            (user_id, send_date),
        ).fetchone()
    return row is not None


def send_digest(
    user_id: str,
    *,
    force: bool = False,
    skip_if_empty: bool = True,
) -> dict:
    """
    Compose + send a digest for one user. Idempotent within a calendar day
    unless force=True. Returns a small status dict.
    """
    try:
        tz = ZoneInfo(DIGEST_TZ_NAME)
    except Exception:
        tz = timezone.utc
    today = datetime.now(tz).strftime("%Y-%m-%d")

    if not force and _already_sent_today(user_id, today):
        return {"ok": False, "reason": "already_sent_today", "date": today}

    to_addr = _user_email(user_id)
    if not to_addr:
        return {"ok": False, "reason": "no_recipient_configured", "user": user_id}

    content = build_digest_content(user_id)
    if skip_if_empty and content["item_count"] == 0:
        # Still record the "no-op" so we don't keep building it
        _record_send(user_id, today, 0, False, "empty_skipped")
        return {"ok": True, "skipped": True, "reason": "no_activity"}

    subject = f"[Insider digest] {content['sections']['watched_events']} watched + {content['sections']['top_moves']} top moves"
    smtp_ok, err = _send(to_addr, subject, content["plain"], content["html"])
    _record_send(user_id, today, content["item_count"], smtp_ok, err)
    return {
        "ok": smtp_ok,
        "to": to_addr,
        "date": today,
        "item_count": content["item_count"],
        "sections": content["sections"],
        "error": err,
    }


def all_users_with_digest_configured() -> list[str]:
    """Walk env vars for ALERTS_USER_* — those are the recipients we know about."""
    out = []
    for k in os.environ.keys():
        if k.startswith("ALERTS_USER_"):
            uid_safe = k[len("ALERTS_USER_"):]
            # Reverse the safe-encoding from _user_email
            out.append(uid_safe.lower())
    return out


def run_daily_pass(*, force: bool = False) -> dict:
    """
    Iterate every recipient and send their digest. Called by the scheduler
    (server.py) at DIGEST_HOUR_LOCAL. Safe to call multiple times — the
    per-user idempotency check keeps it from double-sending.
    """
    users = all_users_with_digest_configured()
    if not users:
        return {"ok": True, "users": 0, "sent": 0, "reason": "no_recipients_configured"}

    sent = skipped = errored = 0
    per_user = []
    for u in users:
        try:
            res = send_digest(u, force=force)
            per_user.append({"user": u, **res})
            if res.get("ok") and not res.get("skipped"):
                sent += 1
            elif res.get("skipped") or res.get("reason") == "already_sent_today":
                skipped += 1
            else:
                errored += 1
        except Exception as e:
            errored += 1
            per_user.append({"user": u, "ok": False, "error": str(e)})
    return {"ok": True, "users": len(users), "sent": sent,
            "skipped": skipped, "errored": errored, "per_user": per_user}


def status_summary() -> dict:
    init_db()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM digest_sends").fetchone()["n"]
        last = c.execute("SELECT MAX(sent_at) AS t FROM digest_sends").fetchone()["t"]
        last_24h = c.execute(
            "SELECT COUNT(*) AS n FROM digest_sends WHERE sent_at >= ?",
            (int(time.time()) - 86400,),
        ).fetchone()["n"]
    return {
        "alert_mode": ALERT_MODE,
        "smtp_available": _smtp_available(),
        "configured_recipients": len(all_users_with_digest_configured()),
        "digest_hour_local": DIGEST_HOUR_LOCAL,
        "digest_tz": DIGEST_TZ_NAME,
        "digests_sent_total": total,
        "digests_sent_last_24h": last_24h,
        "last_sent_at": last,
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Build (but don't send) a sample digest for the 'default' user
        c = build_digest_content(sys.argv[2] if len(sys.argv) > 2 else "default")
        print(c["plain"])
        print()
        print(f"item_count={c['item_count']} sections={c['sections']}")
    else:
        print(json.dumps(status_summary(), indent=2))
