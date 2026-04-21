"""Email subscription fan-out.

Called by the monitoring job (and admin update routes) to notify
everyone subscribed to the affected components about a new incident,
an incident status change, or a resolution.

This module only schedules emails — it never sends inline. Each email
is enqueued as a `send_email` job so the worker handles retries and
BackoffRate without stalling the cron tick.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from status_system import db as status_db


log = logging.getLogger("status.subscriptions")


def _base_url() -> str:
    return os.environ.get("APP_URL", "https://narve.ai").rstrip("/")


def _unsubscribe_url(token: str) -> str:
    return f"{_base_url()}/status/unsubscribe?token={token}"


async def notify_incident_event(
    incident: dict,
    event_type: str,
    *,
    update: dict | None = None,
) -> dict:
    """Enqueue incident notification emails.

    `event_type` is one of: "created", "updated", "resolved".

    Each subscriber whose components list covers any affected component
    (or is "all") receives one email. The email template is chosen by
    event_type. We never inline — all sends go through the job queue.

    Returns {"enqueued": int, "event": str, "incident_id": int}.
    """
    if event_type not in {"created", "updated", "resolved"}:
        raise ValueError(f"invalid event_type: {event_type!r}")

    try:
        from jobs.email_jobs import enqueue_email
    except Exception as exc:
        log.warning("status subscriptions: jobs module unavailable: %s", exc)
        return {"enqueued": 0, "event": event_type, "incident_id": incident.get("id")}

    template_map = {
        "created": "incident_created",
        "updated": "incident_update",
        "resolved": "incident_resolved",
    }
    template = template_map[event_type]

    subscribers = status_db.list_subscribers_for_components(
        incident.get("affected_components", [])
    )

    enqueued = 0
    for sub in subscribers:
        context = _build_context(incident, update=update, subscriber=sub)
        try:
            await enqueue_email(
                to=sub["email"],
                template=template,
                context=context,
                tags=["status", event_type],
            )
            enqueued += 1
        except Exception as exc:
            log.warning(
                "status notify: enqueue failed for %s on incident %s: %s",
                sub["email"], incident.get("id"), exc,
            )

    return {
        "enqueued": enqueued,
        "event": event_type,
        "incident_id": incident.get("id"),
        "template": template,
    }


def _build_context(
    incident: dict, *, update: dict | None, subscriber: dict
) -> dict:
    comps = ", ".join(incident.get("affected_components") or []) or "n/a"
    ctx = {
        "incident_id": incident["id"],
        "incident_title": incident.get("title") or "Service incident",
        "incident_status": incident.get("status") or "investigating",
        "severity": incident.get("severity") or "minor",
        "affected_components": comps,
        "description": incident.get("description") or "",
        "status_url": f"{_base_url()}/status#incident-{incident['id']}",
        "unsubscribe_url": _unsubscribe_url(subscriber["unsubscribe_token"]),
    }
    if update is not None:
        ctx["update_message"] = update.get("message") or ""
        ctx["update_status"] = update.get("status") or ""
    return ctx


def iter_affected_subscribers(components: Iterable[str]) -> list[dict]:
    """Thin wrapper around the DB helper, re-exported so routes/tests
    don't need to import status_system.db directly."""
    return status_db.list_subscribers_for_components(list(components))
