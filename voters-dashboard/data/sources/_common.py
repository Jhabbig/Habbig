"""Shared helpers for ETL pulls.

Two outputs per ETL run, both atomic:
  - data/cache/{name}.json    : machine-friendly, hot-path read by the server.
  - data/snapshot_{name}.yaml : human-readable last-known-good snapshot
    committed to the repo so the service can boot without any network call.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import yaml

SOURCES_DIR = Path(__file__).resolve().parent
DATA_DIR = SOURCES_DIR.parent
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Rate-limiting: never exceed 10 req/sec across all fetchers.
_MIN_GAP_S = 0.10
_last_call_ts = 0.0


def rate_limit() -> None:
    """Sleep just long enough to keep us under 10 req/sec across the process."""
    global _last_call_ts
    now = time.time()
    gap = now - _last_call_ts
    if gap < _MIN_GAP_S:
        time.sleep(_MIN_GAP_S - gap)
    _last_call_ts = time.time()


def _atomic_write(path: Path, body: str) -> Path:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    return path


def write_overlay(name, payload):
    """Write the JSON cache (hot path) AND the YAML snapshot (committed fallback)."""
    out = {"fetched_at": int(time.time())}
    out.update(payload)

    json_path = CACHE_DIR / (name + ".json")
    _atomic_write(json_path, json.dumps(out, indent=2, ensure_ascii=False, sort_keys=False))

    yaml_path = DATA_DIR / ("snapshot_" + name + ".yaml")
    _atomic_write(
        yaml_path,
        yaml.safe_dump(out, sort_keys=False, allow_unicode=True, default_flow_style=False),
    )
    return json_path


def read_existing(name):
    """Return last cache (JSON) if any; else try the committed YAML snapshot."""
    json_path = CACHE_DIR / (name + ".json")
    if json_path.exists():
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    yaml_path = DATA_DIR / ("snapshot_" + name + ".yaml")
    if yaml_path.exists():
        try:
            return yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None
