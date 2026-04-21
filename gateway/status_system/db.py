"""SQLite CRUD layer for the status page.

Every function here either reads or writes one of the four tables created
in migration 020. No orchestration logic — that lives in the cron job and
the route handlers. Keeping the SQL in one place lets the tests mock the
DB without chasing string-concatenated statements through route files.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Optional

import db

from status_system import (
    COMPONENT_KEYS,
    INCIDENT_STATES,
    SEVERITIES,
    STATUSES,
)


# ── incidents ───────────────────────────────────────────────────────────


def create_incident(
    *,
    title: str,
    description: str = "",
    severity: str = "minor",
    affected_components: Optional[list[str]] = None,
    status: str = "investigating",
    auto_created: bool = False,
    created_at: Optional[int] = None,
) -> int:
    """Insert a new incident row and return its ID.

    Also writes the initial incident_update (matching status+description)
    so the timeline always has at least one entry.
    """
    if severity not in SEVERITIES:
        raise ValueError(f"invalid severity: {severity!r}")
    if status not in INCIDENT_STATES:
        raise ValueError(f"invalid status: {status!r}")

    comps = list(affected_components or [])
    for c in comps:
        if c not in COMPONENT_KEYS:
            raise ValueError(f"unknown component: {c!r}")

    now = int(created_at if created_at is not None else time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO incidents "
            "(created_at, resolved_at, severity, affected_components, title, description, status, auto_created) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?, ?)",
            (
                now,
                severity,
                json.dumps(comps),
                title,
                description,
                status,
                1 if auto_created else 0,
            ),
        )
        incident_id = cur.lastrowid
        c.execute(
            "INSERT INTO incident_updates (incident_id, timestamp, status, message) "
            "VALUES (?, ?, ?, ?)",
            (incident_id, now, status, description or "Incident opened."),
        )
    return int(incident_id)


def _row_to_incident(row) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "severity": row["severity"],
        "affected_components": _loads_components(row["affected_components"]),
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "auto_created": bool(row["auto_created"]),
    }


def _loads_components(raw: Any) -> list[str]:
    """Decode the `affected_components` JSON column. Never raises."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (ValueError, TypeError):
        pass
    return []


def get_incident(incident_id: int) -> Optional[dict]:
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
    return _row_to_incident(row) if row else None


def list_recent_incidents(limit: int = 20) -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_incident(r) for r in rows]


def list_open_incidents() -> list[dict]:
    """Incidents that are still active (not yet resolved)."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM incidents WHERE resolved_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_incident(r) for r in rows]


def list_open_incidents_for_component(component: str) -> list[dict]:
    """Open incidents that list `component` in their affected_components.

    We filter in Python since SQLite has no first-class JSON array
    containment; the open-incident count is tiny (usually 0–5) so the
    client-side filter is cheap.
    """
    return [
        i for i in list_open_incidents()
        if component in i["affected_components"]
    ]


def update_incident(
    incident_id: int,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    severity: Optional[str] = None,
    affected_components: Optional[list[str]] = None,
    status: Optional[str] = None,
    resolved_at: Optional[int] = None,
) -> bool:
    """Patch one or more fields on an incident. Returns True if updated.

    Does NOT append an incident_update row — call `add_incident_update`
    separately when you want a timeline entry.
    """
    sets: list[str] = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if severity is not None:
        if severity not in SEVERITIES:
            raise ValueError(f"invalid severity: {severity!r}")
        sets.append("severity = ?")
        params.append(severity)
    if affected_components is not None:
        for c in affected_components:
            if c not in COMPONENT_KEYS:
                raise ValueError(f"unknown component: {c!r}")
        sets.append("affected_components = ?")
        params.append(json.dumps(affected_components))
    if status is not None:
        if status not in INCIDENT_STATES:
            raise ValueError(f"invalid status: {status!r}")
        sets.append("status = ?")
        params.append(status)
    if resolved_at is not None:
        sets.append("resolved_at = ?")
        params.append(int(resolved_at))

    if not sets:
        return False

    params.append(incident_id)
    with db.conn() as c:
        cur = c.execute(
            f"UPDATE incidents SET {', '.join(sets)} WHERE id = ?", params
        )
        return cur.rowcount > 0


def mark_incident_resolved(incident_id: int, *, message: str = "") -> bool:
    """Convenience: set status=resolved, stamp resolved_at, append update."""
    now = int(time.time())
    ok = update_incident(incident_id, status="resolved", resolved_at=now)
    if ok:
        add_incident_update(
            incident_id, status="resolved",
            message=message or "All affected services have returned to normal.",
            timestamp=now,
        )
    return ok


# ── incident_updates ────────────────────────────────────────────────────


def add_incident_update(
    incident_id: int,
    *,
    status: str,
    message: str,
    timestamp: Optional[int] = None,
) -> int:
    """Append a timeline entry. Mirrors `status` back onto the incident row."""
    if status not in INCIDENT_STATES:
        raise ValueError(f"invalid status: {status!r}")

    ts = int(timestamp if timestamp is not None else time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO incident_updates (incident_id, timestamp, status, message) "
            "VALUES (?, ?, ?, ?)",
            (incident_id, ts, status, message),
        )
        # Keep incident.status synchronised with the latest update.
        c.execute(
            "UPDATE incidents SET status = ? WHERE id = ?",
            (status, incident_id),
        )
        return int(cur.lastrowid)


def list_incident_updates(incident_id: int) -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, incident_id, timestamp, status, message "
            "FROM incident_updates WHERE incident_id = ? "
            "ORDER BY timestamp ASC, id ASC",
            (incident_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "incident_id": r["incident_id"],
            "timestamp": r["timestamp"],
            "status": r["status"],
            "message": r["message"],
        }
        for r in rows
    ]


# ── service_health_snapshots ────────────────────────────────────────────


def record_snapshot(
    component: str,
    status: str,
    response_time_ms: Optional[float] = None,
    *,
    timestamp: Optional[int] = None,
) -> int:
    """Insert one snapshot row. Returns the new row ID."""
    if component not in COMPONENT_KEYS:
        raise ValueError(f"unknown component: {component!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status!r}")

    ts = int(timestamp if timestamp is not None else time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO service_health_snapshots "
            "(timestamp, component, status, response_time_ms) VALUES (?, ?, ?, ?)",
            (ts, component, status, response_time_ms),
        )
        return int(cur.lastrowid)


def get_latest_snapshot(component: str) -> Optional[dict]:
    with db.conn() as c:
        row = c.execute(
            "SELECT id, timestamp, component, status, response_time_ms "
            "FROM service_health_snapshots WHERE component = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (component,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "component": row["component"],
        "status": row["status"],
        "response_time_ms": row["response_time_ms"],
    }


def list_snapshots_since(
    component: str, since_ts: int, until_ts: Optional[int] = None
) -> list[dict]:
    until = int(until_ts if until_ts is not None else time.time())
    with db.conn() as c:
        rows = c.execute(
            "SELECT timestamp, status, response_time_ms "
            "FROM service_health_snapshots "
            "WHERE component = ? AND timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp ASC",
            (component, int(since_ts), until),
        ).fetchall()
    return [
        {
            "timestamp": r["timestamp"],
            "status": r["status"],
            "response_time_ms": r["response_time_ms"],
        }
        for r in rows
    ]


def prune_snapshots_older_than(cutoff_ts: int) -> int:
    """Delete snapshots older than `cutoff_ts`. Returns the row count.

    Only exists so a retention cron can keep the table from growing
    forever; the uptime calculation itself is windowed so it doesn't need
    this to be called.
    """
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM service_health_snapshots WHERE timestamp < ?",
            (int(cutoff_ts),),
        )
        return cur.rowcount


# ── status_subscriptions ────────────────────────────────────────────────


def _generate_token() -> str:
    """24-byte URL-safe random token. Orthogonal to the signed-HMAC
    tokens used by email_system/unsubscribe.py — status subscriptions
    don't need HMAC because the token is the primary key (no forgery
    risk: guessing a random 24-byte string is infeasible).
    """
    return secrets.token_urlsafe(24)


def create_subscription(
    email: str,
    components: Any = "all",
    *,
    confirmed: bool = True,
) -> dict:
    """Subscribe `email` to status updates. Idempotent — returns the
    existing token if the email is already subscribed.

    `components` is either the string "all" or a list of component keys.
    """
    email_norm = email.strip().lower()
    if not email_norm or "@" not in email_norm:
        raise ValueError("invalid email")

    if components == "all" or components is None:
        comps_json = '"all"'
    elif isinstance(components, list):
        for c in components:
            if c not in COMPONENT_KEYS:
                raise ValueError(f"unknown component: {c!r}")
        comps_json = json.dumps(components)
    else:
        raise ValueError("components must be 'all' or a list")

    with db.conn() as c:
        row = c.execute(
            "SELECT id, unsubscribe_token FROM status_subscriptions WHERE email = ?",
            (email_norm,),
        ).fetchone()
        if row:
            # Re-activate / update components if the caller passed new ones.
            c.execute(
                "UPDATE status_subscriptions SET components = ?, confirmed = 1 WHERE id = ?",
                (comps_json, row["id"]),
            )
            return {
                "email": email_norm,
                "token": row["unsubscribe_token"],
                "status": "existing",
            }

        token = _generate_token()
        c.execute(
            "INSERT INTO status_subscriptions "
            "(email, subscribed_at, unsubscribe_token, components, confirmed) "
            "VALUES (?, ?, ?, ?, ?)",
            (email_norm, int(time.time()), token, comps_json, 1 if confirmed else 0),
        )
    return {"email": email_norm, "token": token, "status": "new"}


def get_subscription_by_token(token: str) -> Optional[dict]:
    if not token:
        return None
    with db.conn() as c:
        row = c.execute(
            "SELECT id, email, subscribed_at, unsubscribe_token, components, confirmed "
            "FROM status_subscriptions WHERE unsubscribe_token = ?",
            (token,),
        ).fetchone()
    if not row:
        return None
    return _row_to_subscription(row)


def get_subscription_by_email(email: str) -> Optional[dict]:
    email_norm = (email or "").strip().lower()
    if not email_norm:
        return None
    with db.conn() as c:
        row = c.execute(
            "SELECT id, email, subscribed_at, unsubscribe_token, components, confirmed "
            "FROM status_subscriptions WHERE email = ?",
            (email_norm,),
        ).fetchone()
    return _row_to_subscription(row) if row else None


def _row_to_subscription(row) -> dict:
    comps = row["components"]
    try:
        parsed = json.loads(comps) if comps else "all"
    except (ValueError, TypeError):
        parsed = "all"
    return {
        "id": row["id"],
        "email": row["email"],
        "subscribed_at": row["subscribed_at"],
        "unsubscribe_token": row["unsubscribe_token"],
        "components": parsed,
        "confirmed": bool(row["confirmed"]),
    }


def delete_subscription_by_token(token: str) -> bool:
    if not token:
        return False
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM status_subscriptions WHERE unsubscribe_token = ?",
            (token,),
        )
        return cur.rowcount > 0


def list_subscribers_for_components(components: list[str]) -> list[dict]:
    """Return subscribers whose components field covers any of `components`.

    A row with components="all" matches every component. Otherwise we
    filter in Python since SQLite has no JSON containment operator — the
    total subscriber count should remain small enough for this to be
    fine, and callers typically batch the results into a jobs queue.
    """
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, email, subscribed_at, unsubscribe_token, components, confirmed "
            "FROM status_subscriptions WHERE confirmed = 1"
        ).fetchall()

    want = set(components or [])
    out: list[dict] = []
    for r in rows:
        sub = _row_to_subscription(r)
        subcomps = sub["components"]
        if subcomps == "all":
            out.append(sub)
            continue
        if isinstance(subcomps, list) and (not want or want & set(subcomps)):
            out.append(sub)
    return out


def list_all_subscriptions() -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, email, subscribed_at, unsubscribe_token, components, confirmed "
            "FROM status_subscriptions ORDER BY subscribed_at DESC"
        ).fetchall()
    return [_row_to_subscription(r) for r in rows]
