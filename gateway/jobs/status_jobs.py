"""Status page monitoring — runs every minute.

Pipeline for each tick:

    1. Run every probe concurrently (status_system.probes.run_all_probes)
    2. Record a snapshot row per component
    3. Diff against the previous tick:
         - if a component was operational and is now degraded/outage and
           no auto-incident is already open for it → create one
         - if an open auto-incident's affected components have all
           returned to operational → mark resolved and notify
    4. Weekly: prune snapshots older than 120 days so the table stays
       bounded (retention buffer beyond the 90-day uptime window).

Nothing here runs at request time — the only caller is the cron
scheduler. That means a probe's failure can never bring down a user
request; the blast radius is a missed snapshot row.
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Optional

from jobs.registry import register_job, register_cron

from status_system import COMPONENT_KEYS
from status_system import db as status_db
from status_system import probes as status_probes
from status_system import subscriptions as status_subs


log = logging.getLogger("jobs.status")


# If a component is continuously in a bad state for this many ticks and
# no open incident exists, auto-create one. Set to 1 so the first bad
# tick triggers — prediction market users expect fast signal.
AUTO_INCIDENT_THRESHOLD_TICKS = 1

# Retention: keep 120 days of snapshots. The 90-day uptime report only
# reads the last 90 days, so 30 days of headroom lets an admin
# forensic-query back a little further than the UI shows.
SNAPSHOT_RETENTION_SEC = 120 * 86400


@register_job("check_service_health")
async def check_service_health() -> dict:
    """Probe every component, record snapshots, open/close auto-incidents.

    Returns a summary dict for the admin jobs panel.
    """
    now = int(time.time())

    # 1. Run the probes.
    results = await status_probes.run_all_probes()

    # 2. Record snapshots; remember the *previous* status so we can detect
    #    transitions on this tick. Fetch all previous statuses first in
    #    one pass so the DB round-trips aren't serialised.
    previous: dict[str, Optional[str]] = {}
    for key in COMPONENT_KEYS:
        snap = status_db.get_latest_snapshot(key)
        previous[key] = snap["status"] if snap else None

    for key, (status, ms) in results.items():
        status_db.record_snapshot(key, status, ms, timestamp=now)

    # 3. Handle transitions.
    opened = 0
    resolved = 0
    for key, (status, _) in results.items():
        was = previous.get(key)

        # Transition into trouble → open auto-incident if none.
        if status in ("degraded", "outage") and was != status:
            opened += await _maybe_open_auto_incident(key, status)

        # Transition back to healthy → possibly close open auto-incidents.
        if status == "operational" and was in ("degraded", "outage"):
            resolved += await _maybe_resolve_auto_incidents(key)

    # 4. Retention prune — every Monday at 00:*, i.e. ~1/week. We check the
    #    calendar here rather than registering a separate cron so the
    #    logic stays in one file.
    pruned = 0
    wallclock = _dt.datetime.now(_dt.timezone.utc)
    if wallclock.weekday() == 0 and wallclock.hour == 0:
        cutoff = now - SNAPSHOT_RETENTION_SEC
        pruned = status_db.prune_snapshots_older_than(cutoff)

    return {
        "checked_components": list(results.keys()),
        "statuses": {k: v[0] for k, v in results.items()},
        "incidents_opened": opened,
        "incidents_resolved": resolved,
        "snapshots_pruned": pruned,
        "ts": now,
    }


async def _maybe_open_auto_incident(component: str, status: str) -> int:
    """Open an auto-incident for `component` iff none already exists.

    Returns 1 if a new incident was created, else 0.
    """
    existing_open = status_db.list_open_incidents_for_component(component)
    auto_open = [i for i in existing_open if i["auto_created"]]
    if auto_open:
        return 0  # already tracking

    severity = "critical" if status == "outage" else "major"
    pretty = {
        "app": "Web application",
        "api": "API",
        "scraper": "Scraper service",
        "worker": "Background jobs",
        "database": "Database",
        "redis": "Cache layer",
    }.get(component, component.title())
    title = f"{pretty} {status}"
    description = (
        f"Automated monitoring detected the {pretty.lower()} is currently "
        f"reporting a {status} status. Our team is investigating."
    )

    inc_id = status_db.create_incident(
        title=title,
        description=description,
        severity=severity,
        affected_components=[component],
        status="investigating",
        auto_created=True,
    )
    incident = status_db.get_incident(inc_id)

    log.warning(
        "status auto-incident opened: component=%s status=%s incident_id=%d",
        component, status, inc_id,
    )

    try:
        await status_subs.notify_incident_event(incident, event_type="created")
    except Exception as exc:
        log.warning("auto-incident notify failed: %s", exc)

    return 1


async def _maybe_resolve_auto_incidents(component: str) -> int:
    """Mark any open auto-incident whose affected components have all
    returned to operational as resolved. Returns the number closed.

    This is called when `component` flipped back to operational. We
    re-check every affected component for each candidate incident,
    in case the incident was opened for more than one component.
    """
    candidates = [
        i for i in status_db.list_open_incidents_for_component(component)
        if i["auto_created"]
    ]
    closed = 0
    for inc in candidates:
        all_recovered = True
        for c in inc["affected_components"]:
            snap = status_db.get_latest_snapshot(c)
            if snap and snap["status"] != "operational":
                all_recovered = False
                break
        if not all_recovered:
            continue

        ok = status_db.mark_incident_resolved(
            inc["id"],
            message="Automated monitoring now reports all affected services as operational.",
        )
        if ok:
            closed += 1
            log.info("status auto-incident resolved: incident_id=%d", inc["id"])
            try:
                fresh = status_db.get_incident(inc["id"])
                await status_subs.notify_incident_event(fresh, event_type="resolved")
            except Exception as exc:
                log.warning("auto-resolve notify failed: %s", exc)
    return closed


# Fire every minute. minute=None, hour=None = all wildcards → 1/min.
register_cron("check_service_health")
