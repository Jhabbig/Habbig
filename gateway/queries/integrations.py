"""Queries for /admin/integrations — single-pane external-integration health.

Surfaces a snapshot of every third-party service narve.ai depends on, so
ops can answer "is X working right now?" without grepping logs or hopping
between four dashboards.

Eight integrations:
  * Stripe       — env keys, last successful webhook, test vs live mode
  * Anthropic    — env key, last successful call, today's spend vs kill-switch
  * Polymarket   — last successful sync run, 24h error rate
  * Kalshi       — last successful sync run, 24h error rate
  * SMTP         — env vars set, dry-run vs live, presence of relay
  * Sentry       — DSN set, traces sample rate, dashboard URL
  * BetterStack  — Logtail token presence
  * Cloudflare   — tunnel ingress healthy (HEAD /health on local gateway)

Status semantics
    connected — fully configured and verifiable healthy
    degraded  — configured but with recent failures / partial config
    down      — missing env / hard failure on probe

Each integration also exposes a ``details`` dict the row template renders
verbatim. Probes that need outbound HTTP are intentionally **not** run
inside :func:`get_integration_status` — that function is a fast read off
local DB + env, suitable for the page snapshot. The "Test connection"
button hits dedicated probe endpoints in ``admin_integrations_routes``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Optional

import db


log = logging.getLogger("queries.integrations")


# Status constants. Templates compare against these so any rename here
# must update the renderer too.
STATUS_CONNECTED = "connected"
STATUS_DEGRADED = "degraded"
STATUS_DOWN = "down"


# ── Env-var helpers ──────────────────────────────────────────────────────


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _has(name: str) -> bool:
    return bool(_env(name))


# ── Per-integration probes ───────────────────────────────────────────────


def _stripe_status() -> dict[str, Any]:
    """Stripe — env keys + last successful webhook from processed_stripe_events.

    Mode (test vs live) is inferred from the secret-key prefix.
    """
    secret = _env("STRIPE_SECRET_KEY")
    webhook = _env("STRIPE_WEBHOOK_SECRET")

    if not secret:
        return {
            "name": "Stripe",
            "slug": "stripe",
            "status": STATUS_DOWN,
            "summary": "STRIPE_SECRET_KEY not set",
            "details": {
                "Secret key": "missing",
                "Webhook secret": "set" if webhook else "missing",
            },
            "testable": False,
        }

    mode = "live" if secret.startswith("sk_live_") else "test"
    last_ok: Optional[int] = None
    last_error: Optional[int] = None
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT MAX(processed_at) AS ts FROM processed_stripe_events "
                "WHERE error IS NULL AND processed_at IS NOT NULL"
            ).fetchone()
            if row and row["ts"]:
                last_ok = int(row["ts"])
            row2 = c.execute(
                "SELECT MAX(received_at) AS ts FROM processed_stripe_events "
                "WHERE error IS NOT NULL"
            ).fetchone()
            if row2 and row2["ts"]:
                last_error = int(row2["ts"])
    except sqlite3.Error:
        pass

    # Degraded if a failing event landed more recently than the last
    # successful one — Stripe retries automatically so this is recoverable
    # but worth surfacing.
    status = STATUS_CONNECTED
    summary = f"{mode} mode"
    if last_error and (last_ok is None or last_error > last_ok):
        status = STATUS_DEGRADED
        summary = f"{mode} mode · recent webhook error"
    if not webhook:
        status = STATUS_DEGRADED
        summary = f"{mode} mode · webhook secret missing"

    return {
        "name": "Stripe",
        "slug": "stripe",
        "status": status,
        "summary": summary,
        "details": {
            "Mode": mode,
            "Last webhook OK": last_ok,
            "Last webhook error": last_error,
            "Webhook secret": "set" if webhook else "missing",
        },
        "testable": True,
    }


def _anthropic_status() -> dict[str, Any]:
    """Anthropic — env key, last successful call, today's spend vs threshold."""
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "name": "Anthropic",
            "slug": "anthropic",
            "status": STATUS_DOWN,
            "summary": "ANTHROPIC_API_KEY not set",
            "details": {"API key": "missing"},
            "testable": False,
        }

    last_ok: Optional[int] = None
    today_cost = 0.0
    try:
        with db.conn() as c:
            # Last non-cached, non-failed call. The schema may not have a
            # ``failed`` column on older DBs — guard with try/except.
            try:
                row = c.execute(
                    "SELECT MAX(timestamp) AS ts FROM claude_usage_log "
                    "WHERE cached_hit = 0 AND (failed IS NULL OR failed = 0)"
                ).fetchone()
            except sqlite3.Error:
                row = c.execute(
                    "SELECT MAX(timestamp) AS ts FROM claude_usage_log "
                    "WHERE cached_hit = 0"
                ).fetchone()
            if row and row["ts"]:
                last_ok = int(row["ts"])

            # Today's spend (UTC calendar day).
            today_start = int(time.time()) - (int(time.time()) % 86400)
            row2 = c.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total "
                "FROM claude_usage_log WHERE timestamp >= ?",
                (today_start,),
            ).fetchone()
            if row2:
                today_cost = round(float(row2["total"] or 0.0), 4)
    except sqlite3.Error:
        pass

    threshold = float(os.environ.get("CLAUDE_KILL_SWITCH_THRESHOLD_USD", "200"))

    # Kill switch live state via ai.client passthrough.
    kill_active = False
    try:
        from ai import client as _ai_client
        kill_active = bool(_ai_client.get_kill_switch_status().get("active"))
    except Exception:
        pass

    pct = (today_cost / threshold * 100.0) if threshold > 0 else 0.0
    status = STATUS_CONNECTED
    summary = f"${today_cost:.2f} / ${threshold:.0f} today"
    if kill_active:
        status = STATUS_DOWN
        summary = "kill-switch active"
    elif pct >= 100:
        status = STATUS_DOWN
        summary = f"over threshold (${today_cost:.2f})"
    elif pct >= 80:
        status = STATUS_DEGRADED
        summary = f"near threshold ({pct:.0f}%)"

    return {
        "name": "Anthropic",
        "slug": "anthropic",
        "status": status,
        "summary": summary,
        "details": {
            "API key": "set",
            "Last call OK": last_ok,
            "Today spend (USD)": today_cost,
            "Kill threshold (USD)": threshold,
            "Kill switch": "ACTIVE" if kill_active else "off",
        },
        "testable": True,
    }


def _job_health(job_name: str) -> tuple[Optional[int], int, int]:
    """Return (last_ok_ts, fail_count_24h, total_24h) for a registered job.

    Reads ``job_runs`` directly — same source the /admin/jobs dashboard
    uses. Missing tables / SQL errors yield (None, 0, 0) so the integration
    row still renders.
    """
    last_ok: Optional[int] = None
    fail_24h = 0
    total_24h = 0
    cutoff = int(time.time()) - 86400
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT MAX(started_at) AS ts FROM job_runs "
                "WHERE job_name = ? AND ok = 1",
                (job_name,),
            ).fetchone()
            if row and row["ts"]:
                last_ok = int(row["ts"])
            row2 = c.execute(
                "SELECT "
                "  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS fails, "
                "  COUNT(*) AS total "
                "FROM job_runs WHERE job_name = ? AND started_at >= ?",
                (job_name, cutoff),
            ).fetchone()
            if row2:
                fail_24h = int(row2["fails"] or 0)
                total_24h = int(row2["total"] or 0)
    except sqlite3.Error:
        pass
    return last_ok, fail_24h, total_24h


def _polymarket_status() -> dict[str, Any]:
    """Polymarket — last successful sync run + 24h error rate.

    No API key required (read-only public endpoints), so the only
    failure-mode worth surfacing is the sync job itself going sideways.
    """
    last_ok, fails, total = _job_health("sync_polymarket_positions")
    err_rate = round(100.0 * fails / total, 1) if total else 0.0
    status = STATUS_CONNECTED
    summary = f"{err_rate:.1f}% errors · 24h" if total else "no runs · 24h"
    if total == 0:
        status = STATUS_DEGRADED
        summary = "no sync runs in 24h"
    elif err_rate >= 50:
        status = STATUS_DOWN
        summary = f"{err_rate:.0f}% errors · 24h"
    elif err_rate >= 10:
        status = STATUS_DEGRADED
        summary = f"{err_rate:.0f}% errors · 24h"

    return {
        "name": "Polymarket",
        "slug": "polymarket",
        "status": status,
        "summary": summary,
        "details": {
            "API base": os.environ.get(
                "POLYMARKET_API_BASE", "https://clob.polymarket.com"
            ),
            "Last sync OK": last_ok,
            "Runs · 24h": total,
            "Failures · 24h": fails,
        },
        "testable": True,
    }


def _kalshi_status() -> dict[str, Any]:
    """Kalshi — same shape as Polymarket. Auth happens per-user; the job
    row aggregates."""
    last_ok, fails, total = _job_health("sync_kalshi_positions")
    err_rate = round(100.0 * fails / total, 1) if total else 0.0
    status = STATUS_CONNECTED
    summary = f"{err_rate:.1f}% errors · 24h" if total else "no runs · 24h"
    if total == 0:
        status = STATUS_DEGRADED
        summary = "no sync runs in 24h"
    elif err_rate >= 50:
        status = STATUS_DOWN
        summary = f"{err_rate:.0f}% errors · 24h"
    elif err_rate >= 10:
        status = STATUS_DEGRADED
        summary = f"{err_rate:.0f}% errors · 24h"

    return {
        "name": "Kalshi",
        "slug": "kalshi",
        "status": status,
        "summary": summary,
        "details": {
            "API base": os.environ.get(
                "KALSHI_API_BASE",
                "https://trading-api.kalshi.com/trade-api/v2",
            ),
            "Last sync OK": last_ok,
            "Runs · 24h": total,
            "Failures · 24h": fails,
        },
        "testable": True,
    }


def _smtp_status() -> dict[str, Any]:
    """SMTP / email — DRY_RUN > RELAY > SMTP_HOST precedence.

    The integration is "connected" when a usable transport is configured;
    a relay URL or an SMTP_HOST satisfies that. DRY_RUN counts as degraded
    because it means production mail isn't actually sent — useful in dev,
    a footgun if it leaks into prod.
    """
    dry_run = (os.environ.get("EMAIL_DRY_RUN", "false").strip().lower()
               in ("1", "true", "yes"))
    relay = _env("EMAIL_RELAY_URL")
    smtp_host = _env("SMTP_HOST")
    smtp_user = _env("SMTP_USER")
    smtp_password = _env("SMTP_PASSWORD") or _env("SMTP_PASS")

    transport: str
    if dry_run:
        transport = "dry-run"
    elif relay:
        transport = "relay"
    elif smtp_host:
        transport = "smtp"
    else:
        transport = "none"

    status = STATUS_CONNECTED
    if transport == "none":
        status = STATUS_DOWN
        summary = "no transport configured"
    elif transport == "dry-run":
        status = STATUS_DEGRADED
        summary = "EMAIL_DRY_RUN=true"
    elif transport == "smtp" and (not smtp_user or not smtp_password):
        status = STATUS_DEGRADED
        summary = "SMTP_HOST set · credentials missing"
    else:
        summary = f"transport: {transport}"

    return {
        "name": "SMTP",
        "slug": "smtp",
        "status": status,
        "summary": summary,
        "details": {
            "Transport": transport,
            "SMTP_HOST": smtp_host or "—",
            "EMAIL_RELAY_URL": "set" if relay else "—",
            "EMAIL_DRY_RUN": "true" if dry_run else "false",
            "From": os.environ.get("EMAIL_FROM", "noreply@narve.ai"),
        },
        "testable": False,
    }


def _sentry_status() -> dict[str, Any]:
    """Sentry — DSN presence. Recent error count comes via SENTRY_AUTH_TOKEN
    if set; otherwise the row reports configuration only."""
    dsn = _env("SENTRY_DSN")
    public_dsn = _env("SENTRY_DSN_PUBLIC")
    auth_token = _env("SENTRY_AUTH_TOKEN")

    if not dsn:
        return {
            "name": "Sentry",
            "slug": "sentry",
            "status": STATUS_DOWN,
            "summary": "SENTRY_DSN not set",
            "details": {
                "Backend DSN": "missing",
                "Frontend DSN": "set" if public_dsn else "missing",
            },
            "testable": False,
        }

    status = STATUS_CONNECTED
    summary = "backend DSN configured"
    if not public_dsn:
        status = STATUS_DEGRADED
        summary = "frontend DSN missing"

    return {
        "name": "Sentry",
        "slug": "sentry",
        "status": status,
        "summary": summary,
        "details": {
            "Backend DSN": "set",
            "Frontend DSN": "set" if public_dsn else "missing",
            "Auth token": "set" if auth_token else "—",
            "Sample rate": os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1"),
        },
        "testable": False,
    }


def _betterstack_status() -> dict[str, Any]:
    """BetterStack Logtail — any of the three source tokens counts as
    configured; missing all three is the down state."""
    apex_app = _env("LOGTAIL_TOKEN_APP")
    apex_scraper = _env("LOGTAIL_TOKEN_SCRAPER")
    apex_worker = _env("LOGTAIL_TOKEN_WORKER")
    # Subproduct-specific tokens (already in .env.example) — treated as
    # additive coverage; not having them isn't a hard fail.
    sub_whale = _env("LOGTAIL_TOKEN_WHALE")
    sub_central = _env("LOGTAIL_TOKEN_CENTRALBANK")
    sub_health = _env("LOGTAIL_TOKEN_HEALTH")

    apex_set = sum(1 for x in (apex_app, apex_scraper, apex_worker) if x)
    sub_set = sum(1 for x in (sub_whale, sub_central, sub_health) if x)

    if apex_set == 0 and sub_set == 0:
        return {
            "name": "BetterStack",
            "slug": "betterstack",
            "status": STATUS_DOWN,
            "summary": "no Logtail tokens set",
            "details": {
                "Apex tokens": "0 / 3",
                "Subproduct tokens": "0 / 3",
            },
            "testable": False,
        }

    status = STATUS_CONNECTED
    if apex_set < 3:
        status = STATUS_DEGRADED
        summary = f"apex {apex_set}/3 · subproduct {sub_set}/3"
    else:
        summary = f"apex 3/3 · subproduct {sub_set}/3"

    return {
        "name": "BetterStack",
        "slug": "betterstack",
        "status": status,
        "summary": summary,
        "details": {
            "Apex tokens": f"{apex_set} / 3",
            "Subproduct tokens": f"{sub_set} / 3",
        },
        "testable": False,
    }


def _cloudflare_status() -> dict[str, Any]:
    """Cloudflare — tunnel ingress healthy.

    We do not probe the public origin from inside the gateway (that would
    require outbound to ourselves and depend on DNS); instead we surface
    whether the operator wired ``CLOUDFLARE_API_TOKEN`` + ``CLOUDFLARE_ZONE_ID``
    so the DNS sync script can run, and let the "Test connection" button
    trigger a HEAD probe against the local gateway's /health.
    """
    token = _env("CLOUDFLARE_API_TOKEN")
    zone = _env("CLOUDFLARE_ZONE_ID")
    if not token and not zone:
        return {
            "name": "Cloudflare",
            "slug": "cloudflare",
            "status": STATUS_DEGRADED,
            "summary": "API token / zone not set",
            "details": {
                "API token": "missing",
                "Zone ID": "missing",
            },
            "testable": True,
        }
    status = STATUS_CONNECTED if (token and zone) else STATUS_DEGRADED
    summary = "API token + zone set" if (token and zone) else "partial config"
    return {
        "name": "Cloudflare",
        "slug": "cloudflare",
        "status": status,
        "summary": summary,
        "details": {
            "API token": "set" if token else "missing",
            "Zone ID": "set" if zone else "missing",
        },
        "testable": True,
    }


# ── Public aggregator ────────────────────────────────────────────────────


def get_integration_status() -> dict[str, dict[str, Any]]:
    """Snapshot of all 8 integrations. Returns a dict keyed by slug.

    Never raises. Each value carries:
      * name (display label)
      * slug (URL-safe key, matches the dict key)
      * status (connected / degraded / down)
      * summary (one-line headline rendered in the row)
      * details (dict of label -> value the row expands inline)
      * testable (whether the Test connection button renders)
    """
    out: dict[str, dict[str, Any]] = {}
    for fn in (
        _stripe_status,
        _anthropic_status,
        _polymarket_status,
        _kalshi_status,
        _smtp_status,
        _sentry_status,
        _betterstack_status,
        _cloudflare_status,
    ):
        try:
            row = fn()
        except Exception:
            log.exception("integration probe crashed: %s", fn.__name__)
            slug = fn.__name__.strip("_").replace("_status", "")
            row = {
                "name": slug.title(),
                "slug": slug,
                "status": STATUS_DOWN,
                "summary": "probe error",
                "details": {"error": "probe raised"},
                "testable": False,
            }
        out[row["slug"]] = row
    return out


__all__ = [
    "STATUS_CONNECTED",
    "STATUS_DEGRADED",
    "STATUS_DOWN",
    "get_integration_status",
]
