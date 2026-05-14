"""Public status page — /status, /api/status, /status/feed.xml.

Shape:

    server.py  ──┐                         ┌── /status (HTML)
                 │                         ├── /api/status (JSON)
                 ├── status_routes.py ─────┼── /status/feed.xml (RSS)
                 │                         ├── /api/status/subscribe (POST)
                 │                         └── /admin/status + /admin/incidents/*
                 │
    jobs/        ├── status_jobs.py ─── cron: check_service_health (1/min)
                 │                         │
    status_system/                         ▼
      db.py         CRUD for the 4 tables
      probes.py     per-component health checks
      uptime.py     90-day uptime aggregation + daily buckets
      feeds.py      RSS XML generation
      subscriptions.py  signed-token unsubscribe link helpers

Everything under `status_system/` is stateless — it reads/writes the DB
and returns plain Python types. The route layer and cron job compose
these helpers but never embed SQL themselves.
"""

from __future__ import annotations


# Canonical (key, display_name) pairs. Order here determines the order
# on the status page and RSS feed. Keys must match the `component` column
# in service_health_snapshots and the entries in affected_components.
COMPONENTS: list[tuple[str, str]] = [
    ("app", "Web Application"),
    ("api", "API"),
    ("scraper", "Scraper Service"),
    ("worker", "Background Jobs"),
    ("database", "Database"),
    ("redis", "Cache Layer"),
]

COMPONENT_KEYS: tuple[str, ...] = tuple(k for k, _ in COMPONENTS)
COMPONENT_DISPLAY: dict[str, str] = dict(COMPONENTS)


# Canonical status strings. Used everywhere — snapshot rows, the overall
# system banner, and incident records. "outage" is reserved for a full
# component failure; "degraded" covers partial / slow responses.
STATUSES: tuple[str, ...] = ("operational", "degraded", "outage")


# Severity tiers for incidents. "critical" affects core user flows; "major"
# is noticeable but bounded; "minor" is advisory (e.g. one background job
# stalled, site still fully usable). "info" is reserved for non-outage
# announcements — planned maintenance, deploys, feature launches — that
# still belong on the status timeline.
SEVERITIES: tuple[str, ...] = ("info", "minor", "major", "critical")


# Incident lifecycle states. `resolved` and `completed` are terminal; the
# others represent work-in-progress. `resolved` is used for outages that
# returned to normal; `completed` is used for planned events (deploys,
# launches, maintenance) that finished as planned.
INCIDENT_STATES: tuple[str, ...] = (
    "investigating", "identified", "monitoring", "resolved", "completed",
)

# States that mark an incident as no longer open. Used by the route layer
# to decide when to stamp `resolved_at` and stop rendering it as ongoing.
TERMINAL_INCIDENT_STATES: frozenset[str] = frozenset({"resolved", "completed"})
