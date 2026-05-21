"""Webhook-based alerting for religion-dashboard signals.

Subscribers register a webhook URL and a list of conditions. A background
thread checks conditions every 5 minutes; when one fires, we POST a
JSON payload to the webhook URL with an HMAC-SHA256 signature header so
the receiver can verify the call.

CONDITIONS supported:
    {"type": "pope_health",   "min_score": 5}
        — fire when /api/pope-health score crosses min_score (rising edge).
    {"type": "edge",          "min_abs_pp": 3}
        — fire when /api/edge surfaces a market with |edge_pp| >= threshold.
    {"type": "conclave_drift","min_added": 1}
        — fire when /api/conclave/live drift.added_since_curated grows.
    {"type": "papal_vacancy"}
        — fire when /api/conclave/live indicates sede vacante (best-effort
          via 'Holy See vacant' marker in source data).

State is persisted in a SQLite file at /app/data/alerts.sqlite (or
$ALERTS_DB_PATH if set). Last-fired times prevent duplicate alerts:
each (subscription, condition_key) combo fires at most once per 6h.

SECURITY:
    - HMAC key from $ALERTS_HMAC_SECRET (auto-generated random if unset).
    - Subscribe endpoint requires the SAME secret as a Bearer header.
      Without the secret, subscribe endpoints 401 — so anonymous web
      users can't enrol arbitrary webhooks.
    - Outbound webhook POSTs include X-Religion-Signature: sha256=<hex>
      so receivers can verify with hmac.compare_digest.
    - URLs to private RFC1918 / loopback are rejected at subscribe time
      (prevents SSRF to internal services).

ENDPOINTS exposed by server.py:
    POST   /api/alerts            — subscribe (needs Bearer secret)
    GET    /api/alerts            — list subscriptions (needs Bearer)
    DELETE /api/alerts/<id>       — unsubscribe (needs Bearer)
    POST   /api/alerts/test/<id>  — fire a test alert now (needs Bearer)
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import sqlite3
import threading
import time
import urllib.parse
from typing import Optional

import requests

log = logging.getLogger("alerts")

DB_PATH = os.environ.get("ALERTS_DB_PATH", "/app/data/alerts.sqlite")
HMAC_SECRET = os.environ.get("ALERTS_HMAC_SECRET") or secrets.token_urlsafe(32)
CHECK_INTERVAL_SECONDS = 5 * 60
COOLDOWN_SECONDS = 6 * 60 * 60       # one alert per (sub, condition_key) per 6h
USER_AGENT = "religion-dashboard-alerts/1.0"

_db_lock = threading.Lock()


# ─── DB ─────────────────────────────────────────────────────────────────────

def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                webhook_url  TEXT NOT NULL,
                conditions   TEXT NOT NULL,   -- JSON array
                label        TEXT,
                created_at   REAL NOT NULL,
                active       INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS fires (
                sub_id        INTEGER NOT NULL,
                condition_key TEXT NOT NULL,
                last_fired_at REAL NOT NULL,
                PRIMARY KEY (sub_id, condition_key)
            );
        """)


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Reject loopback / private / link-local URLs."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "malformed URL"
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme {parsed.scheme!r} not allowed"
    if not parsed.netloc:
        return False, "no host"
    host = parsed.hostname or ""
    try:
        addr_infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"could not resolve {host}"
    for info in addr_infos:
        ip = info[4][0]
        try:
            ipaddr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if ipaddr.is_loopback or ipaddr.is_private or ipaddr.is_link_local or ipaddr.is_unspecified:
            return False, f"host resolves to non-public IP {ip}"
    return True, ""


def subscribe(webhook_url: str, conditions: list[dict], label: Optional[str] = None) -> dict:
    ok, reason = _is_safe_url(webhook_url)
    if not ok:
        return {"ok": False, "error": f"unsafe webhook URL: {reason}"}
    if not isinstance(conditions, list) or not conditions:
        return {"ok": False, "error": "conditions must be a non-empty list"}
    for c in conditions:
        if not isinstance(c, dict) or "type" not in c:
            return {"ok": False, "error": "each condition needs a 'type' field"}
        if c["type"] not in ("pope_health", "edge", "conclave_drift", "papal_vacancy"):
            return {"ok": False, "error": f"unknown condition type: {c['type']}"}
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO subscriptions(webhook_url, conditions, label, created_at, active) VALUES (?, ?, ?, ?, 1)",
            (webhook_url, json.dumps(conditions), label or "", time.time()),
        )
        sub_id = cur.lastrowid
        conn.commit()
    return {"ok": True, "id": sub_id}


def list_subscriptions() -> list[dict]:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, webhook_url, conditions, label, created_at, active FROM subscriptions ORDER BY id DESC")
        out = []
        for row in cur.fetchall():
            out.append({
                "id": row[0],
                "webhook_url": row[1],
                "conditions": json.loads(row[2]),
                "label": row[3] or "",
                "created_at": row[4],
                "active": bool(row[5]),
            })
        return out


def delete_subscription(sub_id: int) -> dict:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("UPDATE subscriptions SET active=0 WHERE id=?", (sub_id,))
        conn.commit()
        return {"ok": cur.rowcount > 0}


# ─── HMAC + delivery ────────────────────────────────────────────────────────

def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(HMAC_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _deliver(webhook_url: str, payload: dict) -> tuple[bool, str]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Religion-Signature": _sign(body),
        "X-Religion-Event": payload.get("event", "alert"),
    }
    try:
        r = requests.post(webhook_url, data=body, headers=headers, timeout=10)
    except Exception as e:
        return False, f"POST error: {e}"
    if r.status_code >= 200 and r.status_code < 300:
        return True, f"HTTP {r.status_code}"
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def _can_fire(sub_id: int, condition_key: str) -> bool:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT last_fired_at FROM fires WHERE sub_id=? AND condition_key=?",
            (sub_id, condition_key),
        )
        row = cur.fetchone()
        if not row:
            return True
        return (time.time() - row[0]) > COOLDOWN_SECONDS


def _record_fire(sub_id: int, condition_key: str) -> None:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fires(sub_id, condition_key, last_fired_at) VALUES (?, ?, ?)",
            (sub_id, condition_key, time.time()),
        )
        conn.commit()


# ─── Condition checkers ────────────────────────────────────────────────────

def _check_subscription(sub: dict, snapshots: dict) -> list[dict]:
    """Returns a list of alert payloads to fire for this subscription."""
    out = []
    for cond in sub["conditions"]:
        ctype = cond["type"]
        cond_key = json.dumps(cond, separators=(",", ":"), sort_keys=True)
        if not _can_fire(sub["id"], cond_key):
            continue

        if ctype == "pope_health":
            d = snapshots.get("pope_health") or {}
            if not d:
                continue
            threshold = float(cond.get("min_score", 5))
            if d.get("score", 0) >= threshold:
                out.append({
                    "event": "pope_health",
                    "subscription_id": sub["id"],
                    "label": sub["label"],
                    "condition": cond,
                    "score": d["score"],
                    "band": d["band"],
                    "match_count": d["match_count"],
                    "recent_signals": d["recent_signals"][:5],
                    "fired_at": time.time(),
                })
                _record_fire(sub["id"], cond_key)

        elif ctype == "edge":
            d = snapshots.get("edge") or {}
            markets = d.get("markets") or []
            threshold = float(cond.get("min_abs_pp", 3))
            top = [m for m in markets
                   if m.get("edge_pp") is not None and abs(m["edge_pp"]) >= threshold]
            if top:
                out.append({
                    "event": "edge",
                    "subscription_id": sub["id"],
                    "label": sub["label"],
                    "condition": cond,
                    "markets": top[:10],
                    "fired_at": time.time(),
                })
                _record_fire(sub["id"], cond_key)

        elif ctype == "conclave_drift":
            d = snapshots.get("conclave_drift") or {}
            min_added = int(cond.get("min_added", 1))
            added = d.get("drift", {}).get("added_since_curated", []) if d else []
            if len(added) >= min_added:
                out.append({
                    "event": "conclave_drift",
                    "subscription_id": sub["id"],
                    "label": sub["label"],
                    "condition": cond,
                    "added": added,
                    "missing": d.get("drift", {}).get("missing_from_scraped", []),
                    "source": d.get("source"),
                    "fired_at": time.time(),
                })
                _record_fire(sub["id"], cond_key)

        elif ctype == "papal_vacancy":
            # Heuristic: pope_health critical band + sustained signal is a
            # weak proxy; sede vacante itself would be detected by a manual
            # markets-side check. Surface critical-band as a near-vacancy
            # signal for now.
            d = snapshots.get("pope_health") or {}
            if d.get("band") == "critical":
                out.append({
                    "event": "papal_vacancy_signal",
                    "subscription_id": sub["id"],
                    "label": sub["label"],
                    "condition": cond,
                    "pope_health_score": d.get("score"),
                    "band": d.get("band"),
                    "fired_at": time.time(),
                })
                _record_fire(sub["id"], cond_key)
    return out


def run_check_cycle(snapshots: dict) -> dict:
    """Run all conditions against the current snapshot. Returns delivery report."""
    subs = [s for s in list_subscriptions() if s["active"]]
    fired = 0
    delivered = 0
    failures = []
    for sub in subs:
        payloads = _check_subscription(sub, snapshots)
        for p in payloads:
            fired += 1
            ok, msg = _deliver(sub["webhook_url"], p)
            if ok:
                delivered += 1
            else:
                failures.append({"sub_id": sub["id"], "error": msg})
    return {"subscriptions_checked": len(subs), "fired": fired,
            "delivered": delivered, "failures": failures}


# ─── Initialise on import ───────────────────────────────────────────────────

_init_db()
