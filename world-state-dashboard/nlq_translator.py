"""Natural-language query translator.

Turns analyst questions ("russian missile strikes on Ukraine, last 48h") into a
structured filter dict the existing event endpoints can apply. Uses Claude's
structured outputs; falls back to a permissive heuristic when the API is
unavailable.

Returned shape:
    {
        "types":   [str] | None,        # subset of the 10 event types
        "actor_id": str | None,         # gazetteer entity id
        "bbox":    [W, S, E, N] | None, # WGS84 lon/lat box
        "since_offset_sec": int | None, # seconds back from "now"
        "interpretation": str,          # one-sentence summary of applied filters
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import analyst_db
import llm_extractor

_log = logging.getLogger(__name__)

_NLQ_MODEL = os.environ.get("WORLD_STATE_NLQ_MODEL", "claude-opus-4-7").strip()

_EVENT_TYPES = [
    "Strike", "Incident", "Movement", "Sanction", "Deal", "Election",
    "Protest", "Disaster", "Outage", "Statement",
]

_REGION_BBOX = {
    "europe":            (-10.0, 35.0, 40.0, 60.0),
    "middle east":       (25.0,  12.0, 65.0, 42.0),
    "south asia":        (60.0,   5.0, 95.0, 38.0),
    "east asia":         (95.0,  20.0, 145.0, 50.0),
    "southeast asia":    (90.0, -10.0, 145.0, 25.0),
    "africa":            (-20.0, -35.0, 55.0, 38.0),
    "north america":     (-170.0, 15.0, -50.0, 75.0),
    "south america":     (-85.0, -55.0, -30.0, 15.0),
    "red sea":           (32.0,  12.0, 45.0, 30.0),
    "black sea":         (27.0,  40.0, 42.0, 48.0),
    "south china sea":   (105.0,  0.0, 122.0, 25.0),
}


def is_enabled() -> bool:
    return llm_extractor.is_enabled()


# ── Heuristic fallback ──────────────────────────────────────────────────────

_TIME_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"last\s+(\d+)\s*h(?:our|r)s?", re.I), 3600),
    (re.compile(r"last\s+(\d+)\s*d(?:ay)?s?",   re.I), 86400),
    (re.compile(r"last\s+(\d+)\s*w(?:eek)?s?",  re.I), 604800),
    (re.compile(r"past\s+(\d+)\s*h(?:our|r)s?", re.I), 3600),
    (re.compile(r"past\s+(\d+)\s*d(?:ay)?s?",   re.I), 86400),
]

_NAMED_WINDOWS = {
    "today":     86400,
    "this week": 604800,
    "this month": 30 * 86400,
    "24h":       86400,
    "48h":       2 * 86400,
    "72h":       3 * 86400,
}


def _heuristic_translate(q: str) -> dict:
    lower = q.lower()

    # Time window
    since_offset = None
    for pat, unit in _TIME_PATTERNS:
        m = pat.search(lower)
        if m:
            since_offset = int(m.group(1)) * unit
            break
    if since_offset is None:
        for k, v in _NAMED_WINDOWS.items():
            if k in lower:
                since_offset = v
                break

    # Event types — match by token
    type_matches: list[str] = []
    for t in _EVENT_TYPES:
        if t.lower() in lower:
            type_matches.append(t)
    # Common synonyms → types
    syn_map = {
        "airstrike": "Strike", "missile": "Strike", "bomb": "Strike",
        "war": "Strike", "attack": "Incident", "killed": "Incident",
        "deploy": "Movement", "troops": "Movement", "convoy": "Movement",
        "sanction": "Sanction", "embargo": "Sanction",
        "ceasefire": "Deal", "treaty": "Deal", "agreement": "Deal",
        "election": "Election", "vote": "Election",
        "protest": "Protest", "riot": "Protest",
        "earthquake": "Disaster", "flood": "Disaster", "wildfire": "Disaster",
        "blackout": "Outage", "outage": "Outage",
        "said": "Statement", "statement": "Statement", "warned": "Statement",
    }
    for kw, t in syn_map.items():
        if kw in lower and t not in type_matches:
            type_matches.append(t)

    # Actor — match longest alias first against gazetteer
    actor_id = None
    aliases = analyst_db.baseline_aliases()
    aliases.sort(key=lambda t: -len(t[0]))
    for alias, eid in aliases:
        if re.search(r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])", lower):
            actor_id = eid
            break

    # Region bbox
    bbox = None
    for region, b in _REGION_BBOX.items():
        if region in lower:
            bbox = list(b)
            break

    parts = []
    if type_matches:
        parts.append("type=" + "/".join(type_matches))
    if actor_id:
        parts.append("actor=" + actor_id)
    if bbox:
        parts.append("bbox=" + ",".join(f"{x:g}" for x in bbox))
    if since_offset:
        h = since_offset // 3600
        parts.append("window=last %dh" % h)
    interpretation = "Heuristic: " + ("; ".join(parts) if parts else "no filters matched")

    return {
        "types": type_matches or None,
        "actor_id": actor_id,
        "bbox": bbox,
        "since_offset_sec": since_offset,
        "interpretation": interpretation,
    }


# ── LLM translator ──────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    types_list = ", ".join(_EVENT_TYPES)
    gazetteer = "\n".join(
        f"  - {e['id']} — {e['name']} ({e['kind']})"
        for e in analyst_db._BASELINE_ENTITIES
    )
    regions = "\n".join(
        f"  - {k}: {v}" for k, v in _REGION_BBOX.items()
    )
    return f"""You translate analyst questions into structured event filters.

Available event types: {types_list}

Entity gazetteer (use these IDs verbatim when the question names a country / org / person):
{gazetteer}

Common region bounding boxes (use when the question names a region; lon/lat WGS84):
{regions}

OUTPUT FIELDS
- types: array of event types (subset of the list above), or null for "any"
- actor_id: one gazetteer id, or null
- bbox: [west_lon, south_lat, east_lon, north_lat], or null
- since_offset_sec: integer seconds back from now (e.g. 86400 for last 24h), or null
- interpretation: ONE concise sentence describing the filters applied

RULES
- "last N hours/days/weeks" → since_offset_sec = N * (3600|86400|604800)
- "today" = 86400, "this week" = 604800
- When the question names BOTH an actor AND a region, prefer actor_id (it's more specific) and leave bbox null
- When you cannot translate at all, return all-null fields with an interpretation explaining why
- Return ONLY the JSON object, no commentary
"""


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "types": {
                "type": ["array", "null"],
                "items": {"type": "string", "enum": _EVENT_TYPES},
            },
            "actor_id": {"type": ["string", "null"]},
            "bbox": {
                "type": ["array", "null"],
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
            },
            "since_offset_sec": {"type": ["integer", "null"]},
            "interpretation": {"type": "string"},
        },
        "required": ["types", "actor_id", "bbox", "since_offset_sec", "interpretation"],
        "additionalProperties": False,
    }


def translate_query(q: str) -> dict:
    q = (q or "").strip()
    if not q:
        return {"types": None, "actor_id": None, "bbox": None,
                "since_offset_sec": None, "interpretation": "Empty query."}

    if not is_enabled():
        return _heuristic_translate(q)

    client = llm_extractor._get_client()
    if client is None:
        return _heuristic_translate(q)

    try:
        response = client.messages.create(
            model=_NLQ_MODEL,
            max_tokens=600,
            system=[
                {
                    "type": "text",
                    "text": _build_system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": q}],
            output_config={"format": {"type": "json_schema", "schema": _schema()}},
        )
    except Exception as e:
        _log.warning("NLQ LLM call failed (%s) — falling back to heuristic", e)
        return _heuristic_translate(q)

    raw = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            raw = block.text
            break
    if not raw:
        return _heuristic_translate(q)
    try:
        payload = json.loads(raw)
    except Exception:
        return _heuristic_translate(q)

    # Validate actor_id is a known entity (defensive — model could hallucinate).
    if payload.get("actor_id") and not analyst_db.get_entity(payload["actor_id"]):
        payload["actor_id"] = None
    return payload
