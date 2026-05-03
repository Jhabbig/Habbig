"""
Lightweight Prometheus-format metrics for the gateway.

Avoids a dependency on `prometheus_client` — the volume here is small enough
that hand-rolling the text format is cheaper than pulling in a library.

Metric types supported:
- Counter: monotonically increasing (e.g. requests_total).
- Gauge:   point-in-time value (e.g. upstream_healthy, sessions_cached).
- Histogram: bucketed observations (e.g. request_duration_seconds).

Use from server.py:
    from metrics import metrics
    metrics.inc_request("crypto", "GET", 200)
    metrics.observe_request_duration("crypto", 0.123)
    metrics.set_upstream_health("crypto", True)
    print(metrics.render())
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Iterable

# Histogram buckets in seconds — covers fast cache hits up to slow upstream calls.
_DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class _Histogram:
    """Per-label histogram with cumulative buckets (Prometheus convention)."""

    def __init__(self, buckets: Iterable[float] = _DEFAULT_BUCKETS):
        self.buckets = tuple(buckets)
        self.bucket_counts: list[int] = [0] * len(self.buckets)
        self.count = 0
        self.sum = 0.0

    def observe(self, value: float) -> None:
        self.count += 1
        self.sum += value
        for i, b in enumerate(self.buckets):
            if value <= b:
                self.bucket_counts[i] += 1


class Metrics:
    """Thread-safe metrics collector."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started_at = time.time()
        # counter[(name, label_tuple)] -> int
        self._counters: dict[tuple[str, tuple], int] = defaultdict(int)
        # gauge[(name, label_tuple)] -> float
        self._gauges: dict[tuple[str, tuple], float] = {}
        # histogram[(name, label_tuple)] -> _Histogram
        self._histograms: dict[tuple[str, tuple], _Histogram] = {}

    # ── Counter ────────────────────────────────────────────────────────────

    def inc(self, name: str, labels: tuple = (), value: int = 1) -> None:
        with self._lock:
            self._counters[(name, labels)] += value

    def inc_request(self, dashboard: str, method: str, status: int) -> None:
        labels = (("dashboard", dashboard), ("method", method), ("status", str(status)))
        self.inc("gateway_requests_total", labels)

    def inc_cache(self, kind: str, hit: bool) -> None:
        labels = (("kind", kind), ("result", "hit" if hit else "miss"))
        self.inc("gateway_cache_lookups_total", labels)

    def inc_upstream_error(self, dashboard: str, kind: str) -> None:
        labels = (("dashboard", dashboard), ("kind", kind))
        self.inc("gateway_upstream_errors_total", labels)

    # ── Gauge ──────────────────────────────────────────────────────────────

    def set_gauge(self, name: str, value: float, labels: tuple = ()) -> None:
        with self._lock:
            self._gauges[(name, labels)] = value

    def set_upstream_health(self, dashboard: str, healthy: bool) -> None:
        self.set_gauge("gateway_upstream_healthy", 1.0 if healthy else 0.0,
                       (("dashboard", dashboard),))

    # ── Histogram ──────────────────────────────────────────────────────────

    def observe(self, name: str, value: float, labels: tuple = ()) -> None:
        key = (name, labels)
        with self._lock:
            h = self._histograms.get(key)
            if h is None:
                h = _Histogram()
                self._histograms[key] = h
            h.observe(value)

    def observe_request_duration(self, dashboard: str, seconds: float) -> None:
        self.observe("gateway_request_duration_seconds", seconds,
                     (("dashboard", dashboard),))

    # ── Render ─────────────────────────────────────────────────────────────

    @staticmethod
    def _label_str(labels: tuple) -> str:
        if not labels:
            return ""
        parts = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
        return "{" + parts + "}"

    def render(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = {k: v for k, v in self._histograms.items()}
            uptime = time.time() - self._started_at

        # Built-in: process uptime
        lines.append("# HELP gateway_uptime_seconds Seconds since gateway start.")
        lines.append("# TYPE gateway_uptime_seconds gauge")
        lines.append(f"gateway_uptime_seconds {uptime:.3f}")

        # Counters
        names_seen: set[str] = set()
        for (name, labels), value in sorted(counters.items()):
            if name not in names_seen:
                lines.append(f"# TYPE {name} counter")
                names_seen.add(name)
            lines.append(f"{name}{self._label_str(labels)} {value}")

        # Gauges
        names_seen.clear()
        for (name, labels), value in sorted(gauges.items()):
            if name not in names_seen:
                lines.append(f"# TYPE {name} gauge")
                names_seen.add(name)
            lines.append(f"{name}{self._label_str(labels)} {value}")

        # Histograms — emit cumulative buckets, count, sum
        names_seen.clear()
        for (name, labels), h in sorted(histograms.items()):
            if name not in names_seen:
                lines.append(f"# TYPE {name} histogram")
                names_seen.add(name)
            cumulative = 0
            for i, b in enumerate(h.buckets):
                cumulative += h.bucket_counts[i]
                bucket_labels = labels + (("le", _format_bucket(b)),)
                lines.append(f"{name}_bucket{self._label_str(bucket_labels)} {cumulative}")
            inf_labels = labels + (("le", "+Inf"),)
            lines.append(f"{name}_bucket{self._label_str(inf_labels)} {h.count}")
            lines.append(f"{name}_count{self._label_str(labels)} {h.count}")
            lines.append(f"{name}_sum{self._label_str(labels)} {h.sum:.6f}")

        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    """Escape a label value per the Prometheus exposition format."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_bucket(b: float) -> str:
    if b == int(b):
        return str(int(b))
    return f"{b:g}"


# Singleton
metrics = Metrics()
