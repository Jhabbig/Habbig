"""Shared helpers for ingestors: HTTP fetch with timeout/UA + cache primitive."""

from __future__ import annotations

import logging
import time
import urllib.request
from threading import Lock

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (AIRaceDashboard/1.0; +ingestion)"
DEFAULT_TIMEOUT = 12.0


def http_get(url: str, timeout: float = DEFAULT_TIMEOUT, headers: dict | None = None) -> bytes:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (well-known sources)
        return resp.read()


class TTLCache:
    """One-slot TTL cache with thread-safe force option.

    Each ingestor instantiates one of these. The cached payload is the dict
    documented in `ingestion/__init__.py`.
    """

    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._lock = Lock()
        self._payload: dict | None = None
        self._fetched_at: float = 0.0

    def get(self, fetcher, force: bool = False) -> dict:
        now = time.time()
        with self._lock:
            fresh = self._payload is not None and (now - self._fetched_at) < self.ttl
            if fresh and not force:
                return self._payload  # type: ignore[return-value]
        # Fetch outside the lock.
        try:
            payload = fetcher()
            payload.setdefault("ok", True)
            payload.setdefault("error", None)
        except Exception as e:  # noqa: BLE001 — graceful: any failure ⇒ keep last good
            log.warning("ingestion failed: %s", e)
            payload = {
                "fetched_at": now,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "entries": [],
            }
        payload["fetched_at"] = now
        with self._lock:
            # Preserve last successful entries on failure so the UI can keep
            # showing them with an "stale, last fetch failed" badge.
            if not payload.get("ok") and self._payload and self._payload.get("entries"):
                payload["entries"] = self._payload["entries"]
                payload["last_ok_at"] = self._payload.get("last_ok_at", 0)
            else:
                payload["last_ok_at"] = now if payload.get("ok") else self._payload.get("last_ok_at", 0) if self._payload else 0
            self._payload = payload
            self._fetched_at = now
        return payload
