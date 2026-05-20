"""Webhook dispatch for high-confidence insights.

Users register a webhook URL (Discord, Slack, or generic JSON) plus
filters (minimum confidence, minimum |edge|, allowed recommendation
types). After every insight is logged — user-triggered or auto — the
dispatcher checks each enabled webhook against the row and fires the
matching payload asynchronously.

Design choices
--------------
* **Fire in a daemon thread, not inline.** The SSE endpoint must not
  wait on outbound HTTP. We accept that webhooks may not arrive if
  the server crashes mid-fire — that's the right tradeoff for a
  notification feature.
* **Per-webhook failure tracking.** `consecutive_failures` increments
  on every non-2xx; webhooks above a configurable threshold auto-disable
  themselves so a misconfigured URL doesn't burn outbound capacity
  forever.
* **Format per kind.** Discord wants `{content, embeds[]}`; Slack
  wants `{text, blocks[]}`; generic is the full insight JSON.
* **No retry queue.** First attempt only. The matching insight row is
  still in `insight_log` and re-fireable manually via /test.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# A webhook trips into auto-disabled state after this many consecutive
# failures. Tuned conservatively — three retries' worth of attempts is
# enough to handle a brief Discord outage without permanently breaking
# the user's notifications.
AUTO_DISABLE_THRESHOLD = 10

# Outbound HTTP timeout per fire. We have to be aggressive — the
# dispatcher runs in a background thread but the thread pool is small
# and we don't want a hung Slack endpoint to block the next dispatch.
FIRE_TIMEOUT_SECONDS = 8.0

VALID_KINDS = {"discord", "slack", "generic"}

# Confidence ordering for the `min_confidence` filter
_CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


# ─── Filter logic ─────────────────────────────────────────────────────────────

def should_fire(webhook: dict, insight_row: dict) -> bool:
    """Does this insight satisfy this webhook's filter? Pure function
    so the dispatcher can test in a tight loop without DB hits."""
    if not webhook.get("enabled", 1):
        return False
    # Confidence threshold (low | medium | high)
    min_conf = (webhook.get("min_confidence") or "medium").lower()
    have_conf = (insight_row.get("confidence") or "low").lower()
    if _CONF_ORDER.get(have_conf, 0) < _CONF_ORDER.get(min_conf, 1):
        return False
    # Edge threshold
    try:
        min_edge = float(webhook.get("min_abs_edge") or 0.0)
    except (TypeError, ValueError):
        min_edge = 0.0
    edge = insight_row.get("edge")
    if edge is not None and abs(float(edge)) < min_edge:
        return False
    # Recommendation allowlist (comma-separated; empty = all)
    raw_filter = (webhook.get("recommendation_filter") or "").strip()
    if raw_filter:
        allowed = {x.strip().upper() for x in raw_filter.split(",") if x.strip()}
        if insight_row.get("recommendation") not in allowed:
            return False
    return True


# ─── Payload formatters ───────────────────────────────────────────────────────

# Color palette for Discord embeds (one int per recommendation enum).
_DISCORD_COLOR = {
    "BUY_YES":      0x4DD0A8,  # accent green
    "BUY_NO":       0xF0A868,  # warn orange
    "PASS":         0x97A3B0,  # text-muted grey
    "WAIT_AND_SEE": 0x4DD0A8,
}


def format_discord(insight_row: dict, market_url: Optional[str] = None) -> dict:
    """Discord webhook payload — single embed with key fields."""
    rec = insight_row.get("recommendation") or "—"
    conf = insight_row.get("confidence") or "—"
    headline = insight_row.get("headline") or ""
    edge = insight_row.get("edge")
    edge_str = f"{edge*100:+.1f}pp" if edge is not None else "—"
    suggested = insight_row.get("suggested_limit_cents")
    suggested_str = f"{int(suggested)}¢" if suggested is not None else "—"
    auto_tag = " · auto" if insight_row.get("triggered_by") == "auto" else ""

    fields = [
        {"name": "Edge", "value": edge_str, "inline": True},
        {"name": "Confidence", "value": conf, "inline": True},
        {"name": "Limit", "value": suggested_str, "inline": True},
    ]
    if insight_row.get("tail_warning"):
        fields.append({"name": "Tail risk", "value": "flagged", "inline": True})

    embed = {
        "title": f"{rec.replace('_', ' ')}{auto_tag}",
        "description": headline,
        "color": _DISCORD_COLOR.get(rec, 0x97A3B0),
        "fields": fields,
        "footer": {"text": f"narve.ai · {insight_row.get('market_id', '')}"},
    }
    if market_url:
        embed["url"] = market_url
    return {"embeds": [embed]}


def format_slack(insight_row: dict, market_url: Optional[str] = None) -> dict:
    """Slack-compatible payload using block kit."""
    rec = insight_row.get("recommendation") or "—"
    conf = insight_row.get("confidence") or "—"
    headline = insight_row.get("headline") or ""
    edge = insight_row.get("edge")
    edge_str = f"{edge*100:+.1f}pp" if edge is not None else "—"
    suggested = insight_row.get("suggested_limit_cents")
    suggested_str = f"{int(suggested)}¢" if suggested is not None else "—"

    blocks = [
        {"type": "section", "text": {
            "type": "mrkdwn",
            "text": f"*{rec.replace('_', ' ')}* — _{conf}_\n{headline}",
        }},
        {"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": f"edge {edge_str} · limit {suggested_str}"
                     f"{' · tail risk' if insight_row.get('tail_warning') else ''}"
                     f" · `{insight_row.get('market_id', '')}`"},
        ]},
    ]
    payload: dict = {
        "text": f"{rec.replace('_', ' ')} — {headline}",
        "blocks": blocks,
    }
    if market_url:
        payload["blocks"].insert(0, {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{market_url}|View market>"},
        })
    return payload


def format_generic(insight_row: dict, market_url: Optional[str] = None) -> dict:
    """Generic JSON payload — the full row plus an optional URL.

    Useful for in-house webhooks or Zapier-style integrations that
    want to do their own formatting.
    """
    out = dict(insight_row)
    if market_url:
        out["market_url"] = market_url
    return out


_FORMATTERS = {
    "discord": format_discord,
    "slack": format_slack,
    "generic": format_generic,
}


# ─── Persistence ──────────────────────────────────────────────────────────────

def list_webhooks(conn_factory, user_id: str) -> list[dict]:
    """All webhooks owned by one user, oldest first."""
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT id, user_id, url, kind, min_confidence, min_abs_edge,
                      recommendation_filter, enabled, created_at,
                      last_fired_at, last_error, consecutive_failures,
                      total_fires
               FROM insight_webhooks
               WHERE user_id = ?
               ORDER BY created_at ASC""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_webhook(conn_factory, webhook_id: int, user_id: str) -> Optional[dict]:
    """Fetch a single webhook scoped to the caller."""
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT * FROM insight_webhooks
               WHERE id = ? AND user_id = ?""",
            (int(webhook_id), user_id),
        ).fetchone()
    return dict(row) if row else None


def create_webhook(conn_factory, *, user_id: str, url: str, kind: str,
                   min_confidence: str = "medium",
                   min_abs_edge: float = 0.10,
                   recommendation_filter: str = "") -> int:
    """Insert a new webhook row. Caller is expected to have validated
    `url` is HTTPS and `kind` is in VALID_KINDS — both checks happen
    in the endpoint, not here."""
    with conn_factory() as conn:
        cur = conn.execute(
            """INSERT INTO insight_webhooks
                   (user_id, url, kind, min_confidence, min_abs_edge,
                    recommendation_filter, enabled)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (user_id, url, kind, min_confidence,
             float(min_abs_edge), recommendation_filter),
        )
        return cur.lastrowid


def update_webhook(conn_factory, webhook_id: int, user_id: str,
                   **fields) -> bool:
    """Patch the writable fields. Unknown keys are silently dropped to
    keep callers from injecting arbitrary columns."""
    allowed = {"enabled", "min_confidence", "min_abs_edge",
               "recommendation_filter"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return False
    sets = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [int(webhook_id), user_id]
    with conn_factory() as conn:
        cur = conn.execute(
            f"UPDATE insight_webhooks SET {sets} WHERE id = ? AND user_id = ?",
            params,
        )
        return cur.rowcount > 0


def delete_webhook(conn_factory, webhook_id: int, user_id: str) -> bool:
    with conn_factory() as conn:
        cur = conn.execute(
            "DELETE FROM insight_webhooks WHERE id = ? AND user_id = ?",
            (int(webhook_id), user_id),
        )
        return cur.rowcount > 0


def _record_fire(conn_factory, webhook_id: int, ok: bool,
                 error: Optional[str] = None) -> None:
    """Update the webhook's tracking columns after a fire attempt.
    Auto-disables the webhook when it crosses AUTO_DISABLE_THRESHOLD."""
    now_clause = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    try:
        with conn_factory() as conn:
            if ok:
                conn.execute(
                    f"""UPDATE insight_webhooks
                        SET last_fired_at = {now_clause},
                            consecutive_failures = 0,
                            last_error = NULL,
                            total_fires = total_fires + 1
                        WHERE id = ?""",
                    (int(webhook_id),),
                )
            else:
                conn.execute(
                    f"""UPDATE insight_webhooks
                        SET last_fired_at = {now_clause},
                            consecutive_failures = consecutive_failures + 1,
                            last_error = ?,
                            total_fires = total_fires + 1,
                            enabled = CASE
                                WHEN consecutive_failures + 1 >= ?
                                THEN 0 ELSE enabled
                            END
                        WHERE id = ?""",
                    (error or "unknown",
                     AUTO_DISABLE_THRESHOLD, int(webhook_id)),
                )
    except Exception as e:
        logger.warning("webhook _record_fire failed for %d: %s", webhook_id, e)


# ─── Fire path ────────────────────────────────────────────────────────────────

def fire(webhook: dict, insight_row: dict, *,
         market_url: Optional[str] = None,
         conn_factory=None) -> dict:
    """POST the formatted payload to the webhook URL. Catches every
    exception so the dispatcher loop never crashes on a bad URL.

    Returns ``{ok, status, error}`` so tests can assert without
    inspecting webhook log rows.
    """
    kind = (webhook.get("kind") or "generic").lower()
    formatter = _FORMATTERS.get(kind, format_generic)
    payload = formatter(insight_row, market_url=market_url)
    url = webhook.get("url")
    if not url:
        if conn_factory is not None:
            _record_fire(conn_factory, webhook["id"], ok=False,
                         error="missing url")
        return {"ok": False, "status": None, "error": "missing url"}
    try:
        resp = requests.post(url, json=payload, timeout=FIRE_TIMEOUT_SECONDS,
                             headers={"User-Agent": "narve-weather/1.0"})
    except requests.RequestException as e:
        if conn_factory is not None:
            _record_fire(conn_factory, webhook["id"], ok=False,
                         error=type(e).__name__)
        return {"ok": False, "status": None, "error": str(e)}

    ok = 200 <= resp.status_code < 300
    err = None if ok else f"HTTP {resp.status_code}"
    if conn_factory is not None:
        _record_fire(conn_factory, webhook["id"], ok=ok, error=err)
    return {"ok": ok, "status": resp.status_code, "error": err}


def dispatch(conn_factory, insight_row: dict, *,
             market_url: Optional[str] = None) -> int:
    """Find every enabled webhook matching this insight and fire them
    all. Used at insight-log time; returns the count of fires issued
    (whether they succeeded is recorded on each webhook row)."""
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT id, user_id, url, kind, min_confidence, min_abs_edge,
                      recommendation_filter, enabled
               FROM insight_webhooks
               WHERE enabled = 1"""
        ).fetchall()
    n_fired = 0
    for row in rows:
        wh = dict(row)
        if not should_fire(wh, insight_row):
            continue
        fire(wh, insight_row, market_url=market_url, conn_factory=conn_factory)
        n_fired += 1
    return n_fired


def dispatch_async(conn_factory, insight_row: dict, *,
                   market_url: Optional[str] = None) -> None:
    """Same as `dispatch` but in a daemon thread so callers don't
    block. The SSE endpoint and auto loop both use this path."""
    t = threading.Thread(
        target=dispatch,
        args=(conn_factory, insight_row),
        kwargs={"market_url": market_url},
        daemon=True,
    )
    t.start()
