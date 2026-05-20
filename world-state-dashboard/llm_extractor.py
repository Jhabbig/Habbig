"""LLM-based event + entity extractor.

When `ANTHROPIC_API_KEY` is set and `WORLD_STATE_LLM_EXTRACT=1`, this module
extracts richer typed events from RSS items via Claude. Calls fail-closed: any
exception returns an empty list so the heuristic path can still produce events.

Model: defaults to `claude-opus-4-7` (per Anthropic SDK guidance). Override via
`WORLD_STATE_EXTRACTOR_MODEL` env var (e.g. `claude-haiku-4-5` for cheaper
high-volume extraction).

The system prompt is stable across calls — gazetteer + ontology + rules — so
prompt caching kicks in after the first request. Per-request user content
(the batch of news items) sits after the cache breakpoint.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import analyst_db

_log = logging.getLogger(__name__)

_MODEL = os.environ.get("WORLD_STATE_EXTRACTOR_MODEL", "claude-opus-4-7").strip()
_ENABLED = os.environ.get("WORLD_STATE_LLM_EXTRACT", "").strip() == "1"

_EVENT_TYPES = [
    "Strike", "Incident", "Movement", "Sanction", "Deal", "Election",
    "Protest", "Disaster", "Outage", "Statement",
]
_ENTITY_KINDS = ["state", "org", "person", "asset", "place"]
_ROLES = ["subject", "target", "mentioned"]

_client = None
_client_failed = False


def is_enabled() -> bool:
    return _ENABLED and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _get_client():
    """Lazy-init the SDK client. Returns None if the SDK is missing."""
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    try:
        import anthropic
        _client = anthropic.Anthropic()
        return _client
    except Exception as e:
        _log.warning("anthropic SDK init failed (%s) — LLM extraction disabled", e)
        _client_failed = True
        return None


def _gazetteer_lines() -> list[str]:
    """Compact one-line-per-entity gazetteer for the system prompt."""
    out = []
    for e in analyst_db._BASELINE_ENTITIES:
        aliases = ", ".join(e["aliases"][:4])
        out.append(f"  - {e['id']} ({e['kind']}, {e['name']}) — aliases: {aliases}")
    return out


def _build_system_prompt() -> str:
    types_list = ", ".join(_EVENT_TYPES)
    kinds_list = ", ".join(_ENTITY_KINDS)
    roles_list = ", ".join(_ROLES)
    gazetteer = "\n".join(_gazetteer_lines())
    return f"""You are an OSINT analyst extracting typed geopolitical events from news headlines.

ONTOLOGY
- Event types (pick one per event): {types_list}
- Entity kinds: {kinds_list}
- Actor roles: {roles_list}
- Severity: integer 1-4
    1 = routine statement / minor
    2 = notable diplomatic or political event
    3 = active conflict, sanctions, movements
    4 = mass casualties, strikes, major disasters

EXISTING ENTITY GAZETTEER (prefer these IDs when an actor matches):
{gazetteer}

EXTRACTION RULES
1. Each headline may produce ZERO or more events. Filter aggressively — pure punditry, sports, entertainment, and stock-tip filler are NOT events. Only emit events when something happened, was decided, was said by a named actor, or is otherwise newsworthy in a geopolitical / macro / security / climate sense.
2. Each event must have at least one actor.
3. For each actor, use the existing gazetteer ID if it matches (case-insensitive name or alias). For NEW actors (not in the gazetteer), propose an id following the convention `kind:slug` (e.g. `org:imf`, `person:lula`, `asset:cv_eisenhower`, `place:strait_of_hormuz`). Include `kind`. Include `lat`/`lon` only if you are confident in the coordinates.
4. `occurred_at` is ISO 8601 UTC. If the headline implies a time different from the publication date, use that; otherwise use the publication date.
5. `lat`/`lon` is the geographic centroid of the event. Inherit from the most relevant actor when the event location is implicit.
6. `confidence` ∈ [0, 1] reflects how sure you are this is a real, correctly-typed event extracted from a single headline.
7. Use the existing 10 event types only. If nothing fits well, emit `Statement` with low confidence or skip the headline entirely.
8. Keep `summary` ≤ 180 chars, declarative, no hedging language.

OUTPUT
Return a JSON object matching the schema. Set `events` to an empty array when nothing extracts.
"""


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_index": {"type": "integer", "description": "0-based index into the input batch this event came from"},
                        "type": {"type": "string", "enum": _EVENT_TYPES},
                        "summary": {"type": "string"},
                        "occurred_at": {"type": "string", "description": "ISO 8601 UTC"},
                        "lat": {"type": ["number", "null"]},
                        "lon": {"type": ["number", "null"]},
                        "confidence": {"type": "number"},
                        "severity": {"type": "integer", "enum": [1, 2, 3, 4]},
                        "actors": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "kind": {"type": "string", "enum": _ENTITY_KINDS},
                                    "name": {"type": "string"},
                                    "role": {"type": "string", "enum": _ROLES},
                                    "lat": {"type": ["number", "null"]},
                                    "lon": {"type": ["number", "null"]},
                                    "is_new": {"type": "boolean", "description": "True if this is NOT in the existing gazetteer"},
                                },
                                "required": ["id", "kind", "name", "role"],
                                "additionalProperties": False,
                            },
                            "minItems": 1,
                        },
                    },
                    "required": ["source_index", "type", "summary", "occurred_at", "confidence", "severity", "actors"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["events"],
        "additionalProperties": False,
    }


def _format_batch_user_msg(items: list[dict]) -> str:
    lines = ["BATCH:", ""]
    for i, it in enumerate(items):
        pub = (it.get("pub_date") or "").strip()
        publisher = (it.get("source") or "").strip()
        title = (it.get("title") or "").strip()
        desc = (it.get("description") or "").strip()[:280]
        lines.append(f"[{i}] publisher={publisher} | pub_date={pub}")
        lines.append(f"    title: {title}")
        if desc:
            lines.append(f"    desc:  {desc}")
        lines.append("")
    lines.append("Extract typed events for all items above. Return the JSON object now.")
    return "\n".join(lines)


def _parse_dt(s: str) -> float:
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return time.time()


def _parse_pub_date(s: str) -> float:
    if not s:
        return time.time()
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return time.time()


def extract_with_claude(items: list[dict]) -> list[dict]:
    """Extract typed events for a batch of RSS items via Claude.

    Returns a list of event candidate dicts in the same shape as the heuristic
    extractor (`event_extractor.extract_event`), including a `source` dict
    referencing the originating item.

    Returns [] on any error so the caller can fall back to the heuristic.
    """
    if not items:
        return []
    if not is_enabled():
        return []
    client = _get_client()
    if client is None:
        return []

    system = _build_system_prompt()
    user_msg = _format_batch_user_msg(items)
    schema = _schema()

    try:
        # System prompt cached — stable across calls. The user message (per-batch
        # items) sits after the cache breakpoint and is re-processed each call.
        response = client.messages.create(
            model=_MODEL,
            max_tokens=8000,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except Exception as e:
        _log.warning("LLM extraction call failed: %s", e)
        return []

    # Pull the JSON text out of the first text block.
    raw = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            raw = block.text
            break
    if not raw:
        return []

    try:
        payload = json.loads(raw)
    except Exception as e:
        _log.warning("LLM extraction JSON parse failed: %s", e)
        return []

    extracted = payload.get("events") or []
    out: list[dict] = []
    for ev in extracted:
        try:
            idx = int(ev["source_index"])
            if not (0 <= idx < len(items)):
                continue
            src_item = items[idx]
            actors_in = ev.get("actors") or []
            actors_norm: list[tuple[str, str]] = []
            for a in actors_in:
                # If the model proposed a new entity, upsert it into the gazetteer.
                if a.get("is_new"):
                    try:
                        analyst_db.upsert_entity(
                            entity_id=a["id"],
                            kind=a["kind"],
                            name=a["name"],
                            aliases=[a["name"].lower()],
                            lat=a.get("lat"),
                            lon=a.get("lon"),
                        )
                    except Exception as e:
                        _log.warning("upsert new entity failed: %s", e)
                actors_norm.append((a["id"], a.get("role") or "subject"))

            occurred_at = _parse_dt(ev["occurred_at"])
            pub_ts = _parse_pub_date(src_item.get("pub_date") or "")

            out.append({
                "type": ev["type"],
                "summary": ev["summary"][:280],
                "occurred_at": occurred_at,
                "lat": ev.get("lat"),
                "lon": ev.get("lon"),
                "confidence": float(ev.get("confidence") or 0.5),
                "severity": int(ev.get("severity") or 1),
                "actors": actors_norm,
                "source": {
                    "publisher": src_item.get("source") or "",
                    "title": src_item.get("title") or "",
                    "url": src_item.get("link") or "",
                    "snippet": (src_item.get("description") or "")[:280],
                    "published_at": pub_ts,
                },
            })
        except Exception as e:
            _log.warning("LLM event normalization failed: %s", e)
            continue

    # Log usage so the operator can watch cost.
    try:
        u = response.usage
        _log.info(
            "LLM extract: %d events, in=%d cache_read=%d cache_create=%d out=%d",
            len(out), u.input_tokens,
            getattr(u, "cache_read_input_tokens", 0) or 0,
            getattr(u, "cache_creation_input_tokens", 0) or 0,
            u.output_tokens,
        )
    except Exception:
        pass

    return out
