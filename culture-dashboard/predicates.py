"""Predicate language for "alert me when X" rules.

Rules live in a JSON config file (path: `CULTURE_PREDICATES_PATH`, default
`predicates.json` in the dashboard directory). Each rule has a `type`
(currently only `"topic"`), a list of `conditions` (all must pass — implicit
AND), an optional `cooldown_hours` (default 6) and a list of `actions`
(default `["log", "webhook"]`).

A condition is `{field, op, value}`. Supported operators:

  ==, !=, >, >=, <, <=     — numeric / string comparison
  in                       — value-in-list (rule value is a list)
  not_in                   — inverse of `in`
  contains                 — rule value is a single item; field is a list
  contains_all             — rule value is a list; field must contain all
  contains_any             — rule value is a list; field must contain ≥1

For `"topic"` rules the implicit field set is:

  label              str
  spread             int   (number of distinct contributing sources)
  surge_signal       float or null
  sources            list[str]
  sections           list[str]
  markets_count      int   (derived from len(markets))
  market_slugs       list[str]
  has_market         bool  (derived from markets_count > 0)
  min_abs_velocity   float or null
  mispricing_score   float or null

Matches are recorded in `predicate_matches` (rule_name, object_key,
matched_at, payload_json). Re-evaluation in the same cooldown window is a
no-op. If `WATCH_WEBHOOK_URL` is set, each fresh match posts a JSON
payload (same shape as the surge webhook).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import cache

log = logging.getLogger(__name__)


_OPS = {
    "==":  lambda a, b: a == b,
    "!=":  lambda a, b: a != b,
    ">":   lambda a, b: a is not None and a > b,
    ">=":  lambda a, b: a is not None and a >= b,
    "<":   lambda a, b: a is not None and a < b,
    "<=":  lambda a, b: a is not None and a <= b,
    "in":          lambda a, b: a in (b or []),
    "not_in":      lambda a, b: a not in (b or []),
    "contains":    lambda a, b: isinstance(a, (list, tuple)) and b in a,
    "contains_all": lambda a, b: isinstance(a, (list, tuple)) and all(x in a for x in (b or [])),
    "contains_any": lambda a, b: isinstance(a, (list, tuple)) and any(x in a for x in (b or [])),
}


def _config_path() -> Path:
    raw = os.environ.get("CULTURE_PREDICATES_PATH")
    if raw:
        return Path(raw)
    return Path(__file__).parent / "predicates.json"


def load_rules() -> list[dict]:
    path = _config_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rules = data.get("rules") if isinstance(data, dict) else data
        return [_normalise(r) for r in (rules or []) if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001
        log.warning("predicates: failed to load %s: %s", path, e)
        return []


def _normalise(rule: dict) -> dict:
    return {
        "name": str(rule.get("name") or "(unnamed)"),
        "type": rule.get("type") or "topic",
        "conditions": rule.get("conditions") or [],
        "actions": rule.get("actions") or ["log", "webhook"],
        "cooldown_hours": float(rule.get("cooldown_hours") or 6),
    }


def _topic_view(t: dict) -> dict:
    """Project a topic-cluster dict into the field set predicates see."""
    markets = t.get("markets") or []
    return {
        "label": t.get("label"),
        "spread": t.get("spread") or 0,
        "surge_signal": t.get("surge_signal"),
        "sources": t.get("sources") or [],
        "sections": t.get("sections") or [],
        "markets_count": len(markets),
        "market_slugs": [m.get("event_slug") for m in markets if m.get("event_slug")],
        "has_market": len(markets) > 0,
        "min_abs_velocity": t.get("min_abs_velocity"),
        "mispricing_score": t.get("mispricing_score"),
    }


def _evaluate_condition(view: dict, cond: dict) -> bool:
    op = cond.get("op")
    fn = _OPS.get(op)
    if not fn:
        return False
    return bool(fn(view.get(cond.get("field")), cond.get("value")))


def evaluate(rule: dict, obj: dict) -> bool:
    view = _topic_view(obj) if rule["type"] == "topic" else obj
    return all(_evaluate_condition(view, c) for c in rule["conditions"])


async def run_once() -> list[dict]:
    """Walk every topic-rule against the current cluster set; record fresh matches."""
    rules = [r for r in load_rules() if r["type"] == "topic"]
    if not rules:
        return []
    # Lazy import — avoids circularity with edge → topics → cache.
    import edge as edge_mod
    topics = edge_mod.compute_topics_with_markets(limit=50)
    fresh: list[dict] = []
    for rule in rules:
        cooldown_s = rule["cooldown_hours"] * 3600
        for t in topics:
            if not evaluate(rule, t):
                continue
            key = t.get("label") or ""
            if cache.recent_predicate_match(rule["name"], key, within_s=cooldown_s):
                continue
            payload = {
                "rule": rule["name"],
                "label": key,
                "spread": t["spread"],
                "surge_signal": t.get("surge_signal"),
                "sources": t.get("sources"),
                "sections": t.get("sections"),
                "markets_count": len(t.get("markets") or []),
            }
            cache.record_predicate_match(rule["name"], key, payload)
            fresh.append(payload)
            log.info("predicate '%s' matched: %s", rule["name"], key)
    if fresh and os.environ.get("WATCH_WEBHOOK_URL", "").strip():
        await _fire_webhook(fresh)
    return fresh


async def _fire_webhook(matches: list[dict]) -> None:
    url = os.environ["WATCH_WEBHOOK_URL"].strip()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={"type": "culture.predicate_matches",
                                     "ts": time.time(), "matches": matches})
    except Exception as e:  # noqa: BLE001
        log.warning("predicate webhook POST failed: %s", e)
