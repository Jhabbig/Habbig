from __future__ import annotations
"""Alert dispatcher.

Watches the same DB tables that ingesters write to and matches new rows
against `alert_rules`. Matches are written to `alert_deliveries` (one row
per (rule, source) — UNIQUE prevents double-firing) and, if the rule has
a webhook_url, POSTed via aiohttp.

Supported rule_types:
    13d_filed         fires when a 13D/G is filed; if `target` set, must
                      match target_ticker. If `threshold` set, ownership_pct
                      must be >= threshold.
    cluster_buy       fires when /api/cluster-buys would surface a new
                      cluster (>=threshold insiders bought in last 14d).
    whale_move        fires when a watched entity (target=slug) takes any
                      ADD/NEW/EXIT/TRIM action above |delta_value_usd| >=
                      threshold.
    consensus_cross   fires when a ticker's consensus_score crosses
                      `threshold` (positive = bullish cross, negative = bearish).

Email delivery: if SMTP_HOST/PORT/USER/PASSWORD/FROM env vars are all set,
we send directly via smtplib. Otherwise an email-type rule writes a
delivery row with status='skipped_email' so the gateway can pick it up
and own the templates.
"""

import asyncio
import json
import logging
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

import aiohttp

from database import get_conn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Match candidates
# ---------------------------------------------------------------------------

def _candidates_13d(since: str) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT id, accession, target_ticker, target_name,
                      ownership_pct, schedule, intent_class, intent_score,
                      filer_entity_id, fetched_at
                 FROM activist_filings
                WHERE fetched_at >= ?""",
            (since,),
        ).fetchall()]


def _candidates_cluster(min_insiders: int) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT issuer_ticker, issuer_name,
                      COUNT(DISTINCT insider_name) AS n_insiders,
                      SUM(value_usd) AS total_value,
                      MAX(txn_date) AS last_txn,
                      MAX(id) AS source_id
                 FROM insider_txns
                WHERE txn_code='P'
                  AND txn_date >= date('now', '-14 days')
                  AND issuer_ticker IS NOT NULL
                GROUP BY issuer_ticker
               HAVING n_insiders >= ?""",
            (min_insiders,),
        ).fetchall()]


def _candidates_whale_move(slug: str, min_value: float) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT hd.id AS source_id, hd.ticker, hd.action,
                      hd.delta_shares, hd.delta_value_usd, hd.quarter_end,
                      e.parent_name
                 FROM holdings_delta hd
                 JOIN entities e ON e.id=hd.entity_id
                WHERE e.slug=?
                  AND hd.action IN ('ADD','NEW','EXIT','TRIM')
                  AND ABS(COALESCE(hd.delta_value_usd, 0)) >= ?""",
            (slug, min_value),
        ).fetchall()]


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _smtp_configured() -> bool:
    return all(os.environ.get(k) for k in
               ("SMTP_HOST", "SMTP_PORT", "SMTP_USER",
                "SMTP_PASSWORD", "SMTP_FROM"))


def _format_email(rule: dict, payload: dict) -> tuple[str, str]:
    """Render (subject, body) for an alert email. Plain text — alert emails
    are short and we don't want to debug HTML rendering across clients."""
    rt = payload.get("rule_type", "alert")
    target = rule.get("target") or "any"
    if rt == "13d_filed":
        f = payload.get("filing", {})
        subj = f"[Whale] 13D filed on {f.get('target_ticker') or '?'} " \
               f"({f.get('ownership_pct')}%)"
        body = (
            f"A new {f.get('schedule')} filing was just disclosed.\n\n"
            f"  Target:   {f.get('target_name')} ({f.get('target_ticker')})\n"
            f"  Stake:    {f.get('ownership_pct')}%\n"
            f"  Intent:   {f.get('intent_class')} (score {f.get('intent_score')})\n"
            f"  Filed:    {f.get('fetched_at')}\n"
        )
    elif rt == "cluster_buy":
        c = payload.get("cluster", {})
        subj = f"[Whale] Insider cluster buy on {c.get('issuer_ticker')}"
        body = (
            f"{c.get('n_insiders')} insiders bought "
            f"{c.get('issuer_ticker')} ({c.get('issuer_name')}) in the last 14 days.\n"
            f"  Total $: {c.get('total_value')}\n"
            f"  Last txn: {c.get('last_txn')}\n"
        )
    elif rt == "whale_move":
        m = payload.get("move", {})
        subj = f"[Whale] {m.get('parent_name')} {m.get('action')} {m.get('ticker')}"
        body = (
            f"{m.get('parent_name')} just disclosed a {m.get('action')} on "
            f"{m.get('ticker')} for the {m.get('quarter_end')} quarter.\n"
            f"  Δ shares:  {m.get('delta_shares')}\n"
            f"  Δ value $: {m.get('delta_value_usd')}\n"
        )
    elif rt == "consensus_cross":
        s = payload.get("snapshot", {})
        subj = f"[Whale] Consensus cross on {s.get('ticker')} ({s.get('consensus_score'):+.2f})"
        body = (
            f"Smart-money consensus on {s.get('ticker')} crossed your threshold.\n"
            f"  Score:   {s.get('consensus_score'):+.2f}\n"
            f"  Quarter: {s.get('quarter_end')}\n"
        )
    else:
        subj = f"[Whale] alert ({rt})"
        body = json.dumps(payload, indent=2)
    body += f"\nTarget filter: {target}\n"
    return subj, body


def _send_email(to_addr: str, subject: str, body: str) -> tuple[bool, Optional[str]]:
    """Send via SMTP. Returns (ok, error_str). Synchronous — we run from
    the dispatcher's executor since aiosmtplib isn't a hard dep yet."""
    try:
        msg = EmailMessage()
        msg["From"] = os.environ["SMTP_FROM"]
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        host = os.environ["SMTP_HOST"]
        port = int(os.environ["SMTP_PORT"])
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                s.send_message(msg)
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _post_webhook(session: aiohttp.ClientSession,
                        url: str, payload: dict) -> tuple[int, Optional[str]]:
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return r.status, None
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _record(rule_id: int, source_table: str, source_id: int,
            payload: dict, status: str,
            response_code: Optional[int] = None,
            error: Optional[str] = None) -> bool:
    """Write delivery row. UNIQUE (rule_id, source_table, source_id) guarantees
    we don't double-fire. Returns True if newly inserted."""
    fired_at = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO alert_deliveries
                     (rule_id, fired_at, source_table, source_id,
                      payload, delivery_status, response_code, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (rule_id, fired_at, source_table, source_id,
                 json.dumps(payload), status, response_code, error),
            )
            conn.execute(
                "UPDATE alert_rules SET last_fired=? WHERE id=?",
                (fired_at, rule_id),
            )
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------

async def run_dispatcher(window_hours: int = 24) -> dict:
    """Match enabled alert_rules against fresh signals from the last N hours
    and deliver matches that haven't already fired.

    Returns counts so the worker thread can log activity.
    """
    since = (datetime.now(timezone.utc).timestamp() - window_hours * 3600)
    since_iso = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()

    with get_conn() as conn:
        rules = [dict(r) for r in conn.execute(
            "SELECT * FROM alert_rules WHERE enabled=1"
        ).fetchall()]

    if not rules:
        return {"rules": 0, "matches": 0, "delivered": 0}

    matches = 0
    delivered = 0
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for rule in rules:
            rt = rule["rule_type"]
            if rt == "13d_filed":
                cands = _candidates_13d(since_iso)
                threshold = rule["threshold"] or 0
                for c in cands:
                    if rule["target"] and (c["target_ticker"] or "").upper() != rule["target"].upper():
                        continue
                    if (c["ownership_pct"] or 0) < threshold:
                        continue
                    payload = {
                        "rule_type": "13d_filed", "filing": c,
                        "user_id": rule["user_id"],
                    }
                    matches += 1
                    delivered += await _deliver(session, rule, "activist_filings",
                                                int(c["id"]), payload)

            elif rt == "cluster_buy":
                threshold = int(rule["threshold"] or 3)
                cands = _candidates_cluster(threshold)
                for c in cands:
                    if rule["target"] and (c["issuer_ticker"] or "").upper() != rule["target"].upper():
                        continue
                    payload = {
                        "rule_type": "cluster_buy", "cluster": c,
                        "user_id": rule["user_id"],
                    }
                    matches += 1
                    # source_id: the latest insider_txns row id in the cluster.
                    delivered += await _deliver(session, rule, "insider_txns",
                                                int(c["source_id"] or 0), payload)

            elif rt == "whale_move":
                if not rule["target"]:
                    continue
                cands = _candidates_whale_move(rule["target"], float(rule["threshold"] or 0))
                for c in cands:
                    payload = {
                        "rule_type": "whale_move", "move": c,
                        "user_id": rule["user_id"],
                    }
                    matches += 1
                    delivered += await _deliver(session, rule, "holdings_delta",
                                                int(c["source_id"]), payload)

            elif rt == "consensus_cross":
                threshold = float(rule["threshold"] or 0.5)
                with get_conn() as conn:
                    cs = conn.execute(
                        """SELECT id, ticker, quarter_end, consensus_score
                             FROM consensus_snapshots
                            WHERE consensus_score IS NOT NULL
                              AND ((? > 0 AND consensus_score >= ?)
                                   OR (? < 0 AND consensus_score <= ?))""",
                        (threshold, threshold, threshold, threshold),
                    ).fetchall()
                for r in cs:
                    if rule["target"] and r["ticker"] != rule["target"].upper():
                        continue
                    payload = {"rule_type": "consensus_cross", "snapshot": dict(r),
                               "user_id": rule["user_id"]}
                    matches += 1
                    delivered += await _deliver(session, rule, "consensus_snapshots",
                                                int(r["id"]), payload)

    return {"rules": len(rules), "matches": matches, "delivered": delivered}


async def _deliver(session: aiohttp.ClientSession, rule: dict,
                   source_table: str, source_id: int, payload: dict) -> int:
    """Deliver a single match. Returns 1 if newly delivered, 0 if already
    fired or persistence failed."""
    if rule["webhook_url"]:
        status_code, err = await _post_webhook(session, rule["webhook_url"], payload)
        ok = 200 <= status_code < 300
        wrote = _record(int(rule["id"]), source_table, source_id, payload,
                        "sent" if ok else "failed",
                        response_code=status_code or None, error=err)
        return 1 if (wrote and ok) else 0
    if rule["email"]:
        if _smtp_configured():
            subj, body = _format_email(rule, payload)
            ok, err = await asyncio.get_event_loop().run_in_executor(
                None, _send_email, rule["email"], subj, body
            )
            wrote = _record(int(rule["id"]), source_table, source_id, payload,
                            "sent" if ok else "failed",
                            error=err)
            return 1 if (wrote and ok) else 0
        # No SMTP configured — defer to the gateway.
        wrote = _record(int(rule["id"]), source_table, source_id, payload,
                        "skipped_email")
        return 1 if wrote else 0
    wrote = _record(int(rule["id"]), source_table, source_id, payload,
                    "skipped_no_webhook")
    return 1 if wrote else 0
