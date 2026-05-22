"""Watchlists + alert evaluator + delivery.

Three condition types:
  - synthesis_threshold: fire when watched ticker's synthesis score >= X
  - new_signal: fire when ANY new filing/trade lands for a watched ticker
  - high_skill_filer: fire when a high-confidence skilled filer makes a
    filing affecting a watched ticker

Four delivery channels:
  - webhook: generic POST JSON to a URL
  - slack:   POST to a Slack incoming-webhook URL (formatted blocks)
  - discord: POST to a Discord webhook URL (formatted embed)
  - email:   SMTP via SMTP_* env vars

Each rule has a `cooldown_minutes` to prevent re-fire spam — we don't
re-fire the same rule × ticker combo more often than that.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import smtplib
from email.mime.text import MIMEText
from typing import Any

import httpx

import db
import signals as signals_mod
import skill as skill_mod

log = logging.getLogger("alerts")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "whale-tracker@narve.ai")
_DEFAULT_TIMEOUT = 10.0


# ─── Watchlist CRUD ──────────────────────────────────────────────────

def _now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def list_watchlists() -> list[dict]:
    with db.connect() as cx:
        wl = cx.execute("SELECT id, name, created_at FROM watchlist ORDER BY id").fetchall()
        out = []
        for r in wl:
            tickers = cx.execute(
                "SELECT ticker FROM watchlist_ticker WHERE watchlist_id = ? ORDER BY ticker",
                (r["id"],),
            ).fetchall()
            rules = cx.execute(
                "SELECT id, name, condition_type, channel, enabled "
                "FROM alert_rule WHERE watchlist_id = ? ORDER BY id",
                (r["id"],),
            ).fetchall()
            out.append({
                "id": r["id"], "name": r["name"], "created_at": r["created_at"],
                "tickers":  [t["ticker"] for t in tickers],
                "rules":    [dict(rr) for rr in rules],
            })
        return out


def create_watchlist(name: str) -> dict:
    with db.connect() as cx:
        cur = cx.execute(
            "INSERT INTO watchlist (name, created_at) VALUES (?, ?)",
            (name, _now_iso()),
        )
        return {"id": cur.lastrowid, "name": name}


def delete_watchlist(watchlist_id: int) -> None:
    with db.connect() as cx:
        cx.execute("DELETE FROM watchlist_ticker WHERE watchlist_id = ?", (watchlist_id,))
        cx.execute("DELETE FROM alert_rule WHERE watchlist_id = ?", (watchlist_id,))
        cx.execute("DELETE FROM watchlist WHERE id = ?", (watchlist_id,))


def add_ticker(watchlist_id: int, ticker: str) -> None:
    with db.connect() as cx:
        cx.execute(
            "INSERT OR IGNORE INTO watchlist_ticker (watchlist_id, ticker, added_at) "
            "VALUES (?, ?, ?)",
            (watchlist_id, ticker.upper().strip(), _now_iso()),
        )


def remove_ticker(watchlist_id: int, ticker: str) -> None:
    with db.connect() as cx:
        cx.execute(
            "DELETE FROM watchlist_ticker WHERE watchlist_id = ? AND ticker = ?",
            (watchlist_id, ticker.upper().strip()),
        )


def create_rule(watchlist_id: int, *, name: str | None, condition_type: str,
                condition_config: dict, channel: str, channel_config: dict,
                cooldown_minutes: int = 60) -> dict:
    if condition_type not in ("synthesis_threshold", "new_signal", "high_skill_filer"):
        raise ValueError(f"bad condition_type: {condition_type}")
    if channel not in ("webhook", "slack", "discord", "email"):
        raise ValueError(f"bad channel: {channel}")
    with db.connect() as cx:
        cur = cx.execute(
            """
            INSERT INTO alert_rule (
                watchlist_id, name, condition_type, condition_config,
                channel, channel_config, cooldown_minutes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (watchlist_id, name, condition_type, json.dumps(condition_config),
             channel, json.dumps(channel_config), int(cooldown_minutes), _now_iso()),
        )
        return {"id": cur.lastrowid, "watchlist_id": watchlist_id, "name": name}


def delete_rule(rule_id: int) -> None:
    with db.connect() as cx:
        cx.execute("DELETE FROM alert_rule WHERE id = ?", (rule_id,))


def recent_events(limit: int = 100) -> list[dict]:
    with db.connect() as cx:
        rows = cx.execute(
            """
            SELECT e.id, e.rule_id, r.name AS rule_name, r.channel,
                   e.fired_at, e.ticker, e.payload, e.delivered, e.delivery_error
            FROM alert_event e
            JOIN alert_rule r ON r.id = e.rule_id
            ORDER BY e.fired_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Evaluator ───────────────────────────────────────────────────────

def _recent_fire(rule_id: int, ticker: str, cooldown_minutes: int) -> bool:
    """True if we fired this rule × ticker within the cooldown window."""
    with db.connect() as cx:
        row = cx.execute(
            """
            SELECT 1 FROM alert_event
            WHERE rule_id = ? AND ticker = ?
              AND fired_at >= datetime('now', ?)
            LIMIT 1
            """,
            (rule_id, ticker, f"-{int(cooldown_minutes)} minutes"),
        ).fetchone()
    return row is not None


def _record_event(rule_id: int, ticker: str | None, payload: dict,
                  delivered: bool, error: str | None) -> int:
    with db.connect() as cx:
        cur = cx.execute(
            """
            INSERT INTO alert_event (rule_id, fired_at, ticker, payload,
                                     delivered, delivery_error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (rule_id, _now_iso(), ticker, json.dumps(payload),
             1 if delivered else 0, error),
        )
        return cur.lastrowid


async def evaluate_all() -> dict:
    """Evaluate every enabled rule against current state. Returns counts."""
    with db.connect() as cx:
        rules = cx.execute(
            "SELECT * FROM alert_rule WHERE enabled = 1"
        ).fetchall()
        rules = [dict(r) for r in rules]

    fired = 0
    for rule in rules:
        try:
            n = await _evaluate_rule(rule)
            fired += n
        except Exception as e:
            log.exception("rule %s evaluation failed: %s", rule.get("id"), e)
    return {"rules_evaluated": len(rules), "fired": fired}


def _watched_tickers(watchlist_id: int) -> list[str]:
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT ticker FROM watchlist_ticker WHERE watchlist_id = ?",
            (watchlist_id,),
        ).fetchall()
    return [r["ticker"] for r in rows]


async def _evaluate_rule(rule: dict) -> int:
    tickers = _watched_tickers(rule["watchlist_id"])
    if not tickers:
        return 0
    cfg = json.loads(rule.get("condition_config") or "{}")
    fired = 0
    cooldown = int(rule.get("cooldown_minutes") or 60)

    if rule["condition_type"] == "synthesis_threshold":
        threshold = float(cfg.get("threshold", 5.0))
        window = int(cfg.get("window_days", 90))
        for t in tickers:
            s = signals_mod.ticker_synthesis(t, window_days=window)
            score = s.get("synthesis_score") or 0
            if score < threshold:
                continue
            if _recent_fire(rule["id"], t, cooldown):
                continue
            payload = {
                "rule":      rule.get("name") or f"rule-{rule['id']}",
                "ticker":    t,
                "score":     score,
                "breakdown": s.get("synthesis_breakdown") or {},
                "threshold": threshold,
                "summary":   _summary_line(t, s),
            }
            await _deliver(rule, t, payload)
            fired += 1

    elif rule["condition_type"] == "new_signal":
        # Fire if any signal hit the ticker in the last `lookback_minutes`.
        lookback = int(cfg.get("lookback_minutes", 15))
        for t in tickers:
            hits = _new_signal_hits(t, lookback)
            if not hits:
                continue
            if _recent_fire(rule["id"], t, cooldown):
                continue
            payload = {
                "rule":     rule.get("name") or f"rule-{rule['id']}",
                "ticker":   t,
                "lookback_minutes": lookback,
                "hits":     hits,
            }
            await _deliver(rule, t, payload)
            fired += 1

    elif rule["condition_type"] == "high_skill_filer":
        # Fire if any skilled filer made a recent filing affecting watched tickers.
        lookback = int(cfg.get("lookback_minutes", 60))
        for t in tickers:
            skilled_hits = _high_skill_hits(t, lookback)
            if not skilled_hits:
                continue
            if _recent_fire(rule["id"], t, cooldown):
                continue
            payload = {
                "rule":          rule.get("name") or f"rule-{rule['id']}",
                "ticker":        t,
                "skilled_filers": skilled_hits,
            }
            await _deliver(rule, t, payload)
            fired += 1

    return fired


def _summary_line(ticker: str, s: dict) -> str:
    parts = []
    if s.get("insider_buy_count"):  parts.append(f"{s['insider_buy_count']} insider buys")
    if s.get("activist_count"):     parts.append(f"{s['activist_count']} activist filings")
    if s.get("ma_event_count"):     parts.append(f"{s['ma_event_count']} M&A events")
    if s.get("fund_holder_count"):  parts.append(f"{s['fund_holder_count']} fund holders")
    cb, cs = s.get("congress_buy_count", 0), s.get("congress_sell_count", 0)
    if cb or cs: parts.append(f"congress {cb}/{cs}")
    if s.get("options_call_count") or s.get("options_put_count"):
        parts.append(f"opts {s.get('options_call_count',0)}C/{s.get('options_put_count',0)}P")
    return f"{ticker}: " + (", ".join(parts) if parts else "no signals")


def _new_signal_hits(ticker: str, lookback_minutes: int) -> list[dict]:
    cutoff = f"-{int(lookback_minutes)} minutes"
    hits: list[dict] = []
    with db.connect() as cx:
        for r in cx.execute(
            "SELECT 'insider' AS k, accession AS id, filed_at AS at "
            "FROM insider_txn WHERE issuer_ticker = ? AND filed_at >= datetime('now', ?) "
            "LIMIT 5",
            (ticker, cutoff),
        ).fetchall():
            hits.append(dict(r))
        for r in cx.execute(
            "SELECT 'activist' AS k, accession AS id, filed_at AS at "
            "FROM activist_stake WHERE issuer_ticker = ? AND filed_at >= datetime('now', ?) "
            "LIMIT 5",
            (ticker, cutoff),
        ).fetchall():
            hits.append(dict(r))
        for r in cx.execute(
            "SELECT 'ma' AS k, accession AS id, filed_at AS at "
            "FROM ma_event WHERE issuer_ticker = ? AND filed_at >= datetime('now', ?) "
            "LIMIT 5",
            (ticker, cutoff),
        ).fetchall():
            hits.append(dict(r))
        for r in cx.execute(
            "SELECT 'options' AS k, alert_id AS id, alerted_at AS at "
            "FROM options_flow_trade WHERE ticker = ? AND alerted_at >= datetime('now', ?) "
            "LIMIT 5",
            (ticker, cutoff),
        ).fetchall():
            hits.append(dict(r))
        for r in cx.execute(
            "SELECT 'dark_pool' AS k, print_id AS id, executed_at AS at "
            "FROM dark_pool_print WHERE ticker = ? AND executed_at >= datetime('now', ?) "
            "LIMIT 5",
            (ticker, cutoff),
        ).fetchall():
            hits.append(dict(r))
    return hits


def _high_skill_hits(ticker: str, lookback_minutes: int) -> list[dict]:
    cutoff = f"-{int(lookback_minutes)} minutes"
    out: list[dict] = []
    with db.connect() as cx:
        rows = cx.execute(
            """
            SELECT reporter_cik, reporter_name, txn_code, is_buy, filed_at
            FROM insider_txn
            WHERE issuer_ticker = ?
              AND filed_at >= datetime('now', ?)
              AND reporter_cik IS NOT NULL
            """,
            (ticker, cutoff),
        ).fetchall()
        if rows:
            skills = skill_mod.skill_for_filers("insider", [r["reporter_cik"] for r in rows])
            for r in rows:
                s = skills.get(r["reporter_cik"])
                if s and s.get("high_confidence_skilled"):
                    out.append({"kind": "insider", "name": r["reporter_name"],
                                "skill": s, "filed_at": r["filed_at"]})
    return out


# ─── Delivery ────────────────────────────────────────────────────────

async def _deliver(rule: dict, ticker: str, payload: dict) -> None:
    cfg = json.loads(rule.get("channel_config") or "{}")
    err: str | None = None
    try:
        if rule["channel"] == "webhook":
            await _deliver_webhook(cfg, payload)
        elif rule["channel"] == "slack":
            await _deliver_slack(cfg, payload)
        elif rule["channel"] == "discord":
            await _deliver_discord(cfg, payload)
        elif rule["channel"] == "email":
            await _deliver_email(cfg, payload)
        else:
            err = f"unknown channel {rule['channel']}"
    except Exception as e:
        err = str(e)[:200]

    _record_event(rule["id"], ticker, payload, delivered=(err is None), error=err)


async def _deliver_webhook(cfg: dict, payload: dict) -> None:
    url = cfg.get("url")
    if not url:
        raise ValueError("webhook url not configured")
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as cx:
        r = await cx.post(url, json=payload)
        r.raise_for_status()


async def _deliver_slack(cfg: dict, payload: dict) -> None:
    url = cfg.get("url")
    if not url:
        raise ValueError("slack url not configured")
    text = f"*{payload.get('rule')}* · {payload.get('ticker')}: {payload.get('summary') or 'signal'}"
    body = {"text": text, "blocks": [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"`{json.dumps({k: v for k, v in payload.items() if k != 'rule'})[:300]}`"},
        ]},
    ]}
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as cx:
        r = await cx.post(url, json=body)
        r.raise_for_status()


async def _deliver_discord(cfg: dict, payload: dict) -> None:
    url = cfg.get("url")
    if not url:
        raise ValueError("discord url not configured")
    title = f"{payload.get('ticker')} — {payload.get('rule')}"
    desc = payload.get("summary") or "signal"
    body = {"embeds": [{
        "title":       title[:256],
        "description": desc[:2000],
        "fields": [
            {"name": k, "value": str(v)[:1024], "inline": True}
            for k, v in payload.items() if k in ("score", "threshold")
        ],
    }]}
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as cx:
        r = await cx.post(url, json=body)
        r.raise_for_status()


async def _deliver_email(cfg: dict, payload: dict) -> None:
    to = cfg.get("to")
    if not to:
        raise ValueError("email 'to' not configured")
    if not SMTP_HOST:
        raise ValueError("SMTP_HOST not set")
    subject = f"[whale-tracker] {payload.get('ticker')} — {payload.get('rule')}"
    body = json.dumps(payload, indent=2)
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = to

    def _send() -> None:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=_DEFAULT_TIMEOUT) as s:
            s.starttls()
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, [to], msg.as_string())

    await asyncio.to_thread(_send)
