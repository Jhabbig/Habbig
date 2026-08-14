"""Disk-backed cache - atomic-rename writes under ./cache/."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("ct.cache")

_LOCK = threading.Lock()


def _cache_dir() -> Path:
    base = os.environ.get("CT_CACHE_DIR")
    if base:
        return Path(base)
    return Path(__file__).resolve().parent.parent / "cache"


def _key_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in key)
    return _cache_dir() / f"{safe}.json"


def load(key: str, ttl_s: int) -> Optional[Any]:
    p = _key_path(key)
    if not p.exists():
        return None
    try:
        envelope = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Failed to load cache %s: %s", key, e)
        return None
    if not isinstance(envelope, dict):
        return None
    ts = envelope.get("__t__")
    data = envelope.get("__data__")
    if ts is None or data is None:
        return None
    if time.time() - ts > ttl_s:
        return None
    return data


def store(key: str, data: Any) -> None:
    p = _key_path(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Failed to create cache dir: %s", e)
        return
    envelope = {"__t__": time.time(), "__data__": data}
    try:
        with _LOCK, tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(p.parent),
            prefix=p.name + ".", suffix=".tmp", delete=False
        ) as fh:
            json.dump(envelope, fh)
            tmp_path = fh.name
        os.replace(tmp_path, p)
    except OSError as e:
        log.warning("Failed to persist cache %s: %s", key, e)


def all_entries() -> dict[str, dict]:
    out: dict[str, dict] = {}
    d = _cache_dir()
    if not d.exists():
        return out
    now = time.time()
    for f in d.glob("*.json"):
        try:
            stat = f.stat()
        except OSError:
            continue
        out[f.stem] = {"age_s": int(now - stat.st_mtime), "size_b": stat.st_size}
    return out
