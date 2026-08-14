"""Per-source health / freshness scoring. Same pattern as disasters dashboard."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

_LOCK = threading.Lock()
_STATE: dict[str, dict] = {}
_HISTORY: dict[str, deque] = {}
_HIST_LEN = 5


def record_call(source: str, ok: bool, latency_s: float = 0.0,
                http_status: Optional[int] = None) -> None:
    with _LOCK:
        s = _STATE.setdefault(source, {
            "first_seen": time.time(),
            "last_ok_at": None,
            "last_fail_at": None,
            "last_status": None,
            "calls_total": 0,
            "calls_ok": 0,
            "latency_ema_s": None,
        })
        s["calls_total"] += 1
        if ok:
            s["calls_ok"] += 1
            s["last_ok_at"] = time.time()
        else:
            s["last_fail_at"] = time.time()
        s["last_status"] = http_status
        if latency_s > 0:
            prev = s["latency_ema_s"]
            s["latency_ema_s"] = latency_s if prev is None else (0.8 * prev + 0.2 * latency_s)
        h = _HISTORY.setdefault(source, deque(maxlen=_HIST_LEN))
        h.append(ok)


def status(source: str) -> str:
    with _LOCK:
        s = _STATE.get(source)
        if not s:
            return "UNKNOWN"
        h = _HISTORY.get(source) or deque()
        last_ok = s.get("last_ok_at")
        if last_ok and time.time() - last_ok < 60:  # 1 min - crypto data is hot
            return "GREEN"
        if all(not v for v in h) and len(h) == _HIST_LEN:
            return "RED"
        if last_ok and time.time() - last_ok < 600:  # 10 min
            return "YELLOW"
        return "RED"


def all_sources() -> list[dict]:
    with _LOCK:
        out: list[dict] = []
        now = time.time()
        for source, s in _STATE.items():
            last_ok = s.get("last_ok_at")
            last_fail = s.get("last_fail_at")
            out.append({
                "source": source,
                "status": status(source),
                "calls_total": s.get("calls_total"),
                "calls_ok": s.get("calls_ok"),
                "last_ok_age_s": int(now - last_ok) if last_ok else None,
                "last_fail_age_s": int(now - last_fail) if last_fail else None,
                "last_http_status": s.get("last_status"),
                "latency_ema_ms": int((s.get("latency_ema_s") or 0) * 1000),
            })
        out.sort(key=lambda r: r["source"])
        return out
