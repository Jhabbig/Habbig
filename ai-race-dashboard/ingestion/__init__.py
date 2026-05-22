"""Ingestion package — pulls live benchmark data from public leaderboards.

Each module exposes:
  - SOURCE_KEY:        str   — short id (e.g. "lmarena").
  - SOURCE_NAME:       str   — display label.
  - BENCHMARK_KEY:     str   — which `BENCHMARKS` row in data.py this maps to.
  - get_cached(force)  → dict
        {
          "source": SOURCE_KEY,
          "benchmark": BENCHMARK_KEY,
          "fetched_at": float,    # unix seconds; 0 means never
          "ok": bool,
          "error": str|None,
          "entries": [{"model": str, "score": float}],  # raw, un-matched
        }

The merge layer (`live_data.py`) is responsible for matching `model` names
back to rows in `data.MODELS` and overlaying scores.
"""

from . import lmarena, openllm, swebench

ALL_SOURCES = [lmarena, openllm, swebench]


def refresh_all(force: bool = True) -> list[dict]:
    """Fetch every source. Used by the background refresher and /api/refresh."""
    return [src.get_cached(force=force) for src in ALL_SOURCES]


def get_status() -> list[dict]:
    """Per-source status without forcing a refetch."""
    return [src.get_cached(force=False) for src in ALL_SOURCES]
