"""Market-mover detection job + delivery.

Runs every 5 minutes. Delegates detection to
``backend.markets.movement_detector.run_detection_once``; this job
handles delivery of new events (notifications + push).

Events are "new" while ``notified_at`` is NULL. After delivery we stamp
notified_at so the next tick doesn't re-ping.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.movement")


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


@register_job("detect_market_movements")
async def detect_market_movements() -> dict[str, Any]:
    """Run detection, then fan pending events out to subscribers."""
    from backend.markets.movement_detector import run_detection_once
    detection = run_detection_once()
    delivery = await _deliver_pending_events()
    return {"detection": detection, "delivery": delivery}


async def _deliver_pending_events() -> dict[str, Any]:
    conn = _connect()
    try:
        if not _table_exists(conn, "market_movement_events"):
            return {"skipped": "no events table"}
        pending = conn.execute(
            "SELECT * FROM market_movement_events WHERE notified_at IS NULL "
            "ORDER BY detected_at ASC LIMIT 200"
        ).fetchall()
        if not pending:
            return {"pending": 0, "delivered": 0}

        # Match each event against user_market_alerts.
        delivered = 0
        user_rules = []
        if _table_exists(conn, "user_market_alerts"):
            user_rules = [dict(r) for r in conn.execute(
                "SELECT * FROM user_market_alerts WHERE is_active = 1"
            ).fetchall()]

        for event in pending:
            event_dict = dict(event)
            context = {}
            if event_dict.get("narve_context_json"):
                try:
                    context = json.loads(event_dict["narve_context_json"])
                except json.JSONDecodeError:
                    context = {}

            matched_users = _match_users(event_dict, user_rules, context)
            if matched_users:
                _enqueue_push(matched_users, event_dict, context)
                _enqueue_inapp(matched_users, event_dict, context)
                delivered += len(matched_users)

            conn.execute(
                "UPDATE market_movement_events SET notified_at = ? WHERE id = ?",
                (int(time.time()), event_dict["id"]),
            )
        conn.commit()
        return {"pending": len(pending), "delivered": delivered}
    finally:
        conn.close()


def _match_users(event: dict, rules: list[dict], context: dict) -> list[int]:
    matched: set[int] = set()
    for rule in rules:
        if rule.get("alert_type") and rule["alert_type"] != event["event_type"]:
            continue
        if rule.get("market_slug") and rule["market_slug"] != event["market_slug"]:
            continue
        if rule.get("only_when_predictions_exist") and not context.get("prediction_count"):
            continue
        if rule.get("min_predictor_credibility"):
            best = (context.get("best_source") or {}).get("credibility") or 0.0
            if best < rule["min_predictor_credibility"]:
                continue
        if event["event_type"] == "odds_movement" and rule.get("min_movement_pct"):
            if abs(event.get("magnitude") or 0) < rule["min_movement_pct"]:
                continue
        if event["event_type"] == "volume_spike" and rule.get("min_volume_multiple"):
            if (event.get("magnitude") or 0) < rule["min_volume_multiple"]:
                continue
        matched.add(int(rule["user_id"]))
    return sorted(matched)


def _enqueue_push(user_ids: list[int], event: dict, context: dict) -> None:
    try:
        from push import send_push  # type: ignore
    except ImportError:
        return
    for uid in user_ids:
        try:
            send_push(
                user_id=uid,
                title=f"Market move: {event['event_type']}",
                body=f"{event['market_slug']} · Δ {event.get('magnitude')}",
                data={"market_slug": event["market_slug"], "event_type": event["event_type"]},
            )
        except Exception as exc:
            log.warning("push to user %s failed: %s", uid, exc)


def _enqueue_inapp(user_ids: list[int], event: dict, context: dict) -> None:
    try:
        import notifications  # type: ignore
    except ImportError:
        return
    for uid in user_ids:
        try:
            notifications.create_notification(
                user_id=uid,
                kind="market_movement",
                title=f"Market move: {event['event_type']}",
                body=f"{event['market_slug']}",
                data={"event_id": event["id"]},
            )
        except Exception as exc:
            log.warning("in-app notification for user %s failed: %s", uid, exc)


# Every 5 minutes: */5. The registry cron scheduler registers one entry
# per fire time. Register the 12 slots explicitly — this is the same
# pattern existing cron jobs use and keeps the scheduler dumb.
for _min in range(0, 60, 5):
    register_cron("detect_market_movements", minute=_min)
