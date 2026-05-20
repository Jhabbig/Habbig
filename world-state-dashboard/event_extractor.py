"""Event extractor — heuristic + optional LLM path.

Takes a news/feed item (publisher, title, description, published_at) and emits
zero or more typed events. Two backends:

1. Heuristic (default) — keyword + alias matching against the analyst_db
   gazetteer. Pure-stdlib, no LLM dependency.
2. LLM (opt-in) — Claude-based extraction with richer entity / event typing.
   Enabled when ANTHROPIC_API_KEY is set and WORLD_STATE_LLM_EXTRACT=1.

`extract_batch()` is the public entry point — it tries LLM first when enabled
and falls back to the heuristic on any failure, so the dashboard always
produces some events even if the API is down.
"""

from __future__ import annotations

import logging
import re
from email.utils import parsedate_to_datetime
from datetime import timezone

from analyst_db import baseline_aliases, get_entity
import llm_extractor

_log = logging.getLogger(__name__)

# Event type → indicator keywords. Order matters: first match wins.
# Keywords are lowercased substring matches against title + description.
_EVENT_RULES: list[tuple[str, list[str], int]] = [
    # type             keywords                                                 severity
    ("Strike",   ["strike", "airstrike", "missile", "drone strike",
                  "shelling", "bomb", "bombed", "rocket", "shelled"], 4),
    ("Incident", ["killed", "dead", "wounded", "injured", "casualt",
                  "assassinat", "attack", "ambush"], 3),
    ("Movement", ["deploy", "deployed", "advance", "withdraw", "withdrawn",
                  "reinforc", "troops", "convoy", "amassing", "mobiliz"], 3),
    ("Sanction", ["sanction", "embargo", "blacklist", "designat",
                  "frozen assets", "export control"], 3),
    ("Deal",     ["agreement", "treaty", "ceasefire", "truce",
                  "deal signed", "accord", "pact"], 2),
    ("Election", ["election", "elected", "ballot", "vote count",
                  "polls open", "runoff"], 2),
    ("Protest",  ["protest", "demonstration", "rally", "march",
                  "riot", "uprising"], 2),
    ("Disaster", ["earthquake", "tsunami", "flood", "wildfire",
                  "hurricane", "typhoon", "volcano", "eruption", "landslide"], 4),
    ("Outage",   ["blackout", "outage", "power cut", "grid failure",
                  "internet outage", "cable cut"], 3),
    ("Statement",["said", "stated", "warned", "vowed", "condemn",
                  "called for", "denounc", "announc", "press conference"], 1),
]

_TYPE_FALLBACK = "Statement"

# Compiled alias matcher built lazily (after DB seed).
_alias_index: list[tuple[str, str]] | None = None


def _ensure_index() -> list[tuple[str, str]]:
    global _alias_index
    if _alias_index is None:
        # Sort longest-first so "south korea" wins over "korea".
        idx = baseline_aliases()
        idx.sort(key=lambda t: -len(t[0]))
        _alias_index = idx
    return _alias_index


def _classify(text: str) -> tuple[str, int, int]:
    """Return (event_type, indicator_hits, severity)."""
    lower = text.lower()
    for etype, keywords, sev in _EVENT_RULES:
        hits = sum(1 for k in keywords if k in lower)
        if hits:
            return etype, hits, sev
    return _TYPE_FALLBACK, 0, 1


def _match_actors(text: str) -> list[str]:
    lower = " " + text.lower() + " "
    found: dict[str, None] = {}  # ordered set
    consumed: list[tuple[int, int]] = []
    for alias, entity_id in _ensure_index():
        # word-boundary-ish match using surrounding spaces / punctuation
        pattern = re.compile(r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])")
        for m in pattern.finditer(lower):
            span = (m.start(), m.end())
            if any(s <= span[0] < e or s < span[1] <= e for s, e in consumed):
                continue
            consumed.append(span)
            found.setdefault(entity_id, None)
            break
    return list(found.keys())


def _parse_pub_date(s: str) -> float | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def extract_event(item: dict) -> dict | None:
    """Heuristic extraction from one RSS item.

    Returns a candidate event dict or None if nothing actionable was detected.
    Shape:
        {
            "type": str,
            "summary": str,
            "occurred_at": float (epoch seconds),
            "lat": float | None,
            "lon": float | None,
            "confidence": float in [0, 1],
            "severity": int,
            "actors": [(entity_id, role), ...],
            "source": {"publisher", "title", "url", "snippet", "published_at"},
        }
    """
    title = (item.get("title") or "").strip()
    desc = (item.get("description") or "").strip()
    if not title:
        return None

    text = f"{title}. {desc}".strip()
    etype, hits, severity = _classify(text)
    actor_ids = _match_actors(text)

    # Need at least one actor *or* a strong keyword signal to emit anything;
    # otherwise it's just generic news, not a typed event.
    if not actor_ids and hits == 0:
        return None

    # Geo: take the first actor that has coordinates.
    lat = lon = None
    actors: list[tuple[str, str]] = []
    for i, eid in enumerate(actor_ids[:6]):
        ent = get_entity(eid)
        if ent and lat is None and ent.get("lat") is not None:
            lat, lon = ent["lat"], ent["lon"]
        role = "subject" if i == 0 else ("target" if i == 1 else "mentioned")
        actors.append((eid, role))

    confidence = min(1.0, 0.30 + 0.15 * hits + 0.10 * len(actor_ids))
    occurred_at = _parse_pub_date(item.get("pub_date") or "") or 0.0
    if not occurred_at:
        import time as _t
        occurred_at = _t.time()

    return {
        "type": etype,
        "summary": title,
        "occurred_at": occurred_at,
        "lat": lat,
        "lon": lon,
        "confidence": round(confidence, 3),
        "severity": severity,
        "actors": actors,
        "source": {
            "publisher": item.get("source") or "",
            "title": title,
            "url": item.get("link") or "",
            "snippet": desc[:280],
            "published_at": occurred_at,
        },
    }


def _extract_batch_heuristic(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        try:
            ev = extract_event(it)
        except Exception as e:  # noqa: BLE001
            _log.warning("heuristic extractor failed on item: %s", e)
            continue
        if ev:
            out.append(ev)
    return out


# LLM batch size — keep small so a single failure doesn't lose many items, and
# so the user message fits well within Claude's context.
_LLM_BATCH_SIZE = 12


def extract_batch(items: list[dict]) -> list[dict]:
    """Extract events from a batch of items.

    Prefers the LLM path when enabled; falls back to the heuristic for any
    items the LLM couldn't process (or all items if the LLM is disabled or
    errors out). LLM and heuristic outputs share the same shape, so the caller
    doesn't need to know which path produced an event.
    """
    if not items:
        return []

    if not llm_extractor.is_enabled():
        return _extract_batch_heuristic(items)

    # Run the LLM in chunks. Items the LLM doesn't emit an event for fall
    # through to the heuristic — better to get a weaker event than none.
    llm_events: list[dict] = []
    covered_titles: set[str] = set()
    for i in range(0, len(items), _LLM_BATCH_SIZE):
        chunk = items[i:i + _LLM_BATCH_SIZE]
        try:
            evs = llm_extractor.extract_with_claude(chunk)
        except Exception as e:
            _log.warning("LLM extract chunk failed: %s", e)
            evs = []
        for ev in evs:
            llm_events.append(ev)
            covered_titles.add(ev["source"]["title"])

    # Heuristic fills in anything the LLM dropped.
    leftover = [it for it in items if (it.get("title") or "") not in covered_titles]
    heuristic_events = _extract_batch_heuristic(leftover)

    return llm_events + heuristic_events
