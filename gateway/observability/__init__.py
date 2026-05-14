"""Observability helpers: Sentry init + scrubbers."""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from observability.sentry_setup import init_sentry, scrub_sensitive_data, set_user_context, tag_request  # noqa: F401


@lru_cache(maxsize=1)
def detect_release() -> str:
    """Resolve the Sentry release string for this process.

    Order of precedence:
      1. ``NARVE_RELEASE`` env var (set explicitly by the deploy pipeline).
      2. ``git rev-parse --short HEAD`` against the repo root — works
         automatically on any host where the working tree is a git checkout.
      3. ``"unknown"`` as a last-resort fallback so Sentry init never crashes.

    Cached via ``lru_cache`` so we only shell out to git once per process.
    """
    env = os.getenv("NARVE_RELEASE", "").strip()
    if env:
        return env
    try:
        # __file__ → gateway/observability/__init__.py
        # parents[2]   → repo root (gateway/observability → gateway → repo)
        repo_root = Path(__file__).resolve().parents[2]
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
        sha = out.strip()
        if sha:
            return sha
    except Exception:
        pass
    return "unknown"
