"""Shared helpers for ETL pulls."""
from __future__ import annotations

import json
import time
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def write_overlay(name: str, payload: dict) -> Path:
    """Write {by_iso: {...}, fetched_at: ...} to data/cache/{name}.json atomically."""
    out = {
        "fetched_at": int(time.time()),
        **payload,
    }
    target = CACHE_DIR / f"{name}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return target


def read_existing(name: str) -> dict | None:
    """Read a previous overlay if any. Used to keep last-known-good on failure."""
    target = CACHE_DIR / f"{name}.json"
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
