"""Shared Anthropic SDK wrapper + usage accounting.

Every ai/* module calls Claude through ``get_async_client()`` so we have
one place that enforces:

  - reading ANTHROPIC_API_KEY out of the environment
  - falling back to None when the SDK isn't installed or the key is
    missing (tests never hit the network; callers must handle None)
  - recording every response (including cache hits + failures) into
    ``claude_usage_log`` so the admin dashboard and cost alert see
    consistent data

Model ids are held here so ops can swap Haiku/Sonnet versions via env
vars without touching each feature module.

The DB write path is resilient — a logging failure must never crash the
Claude call it was trying to record. ``log_claude_usage_row`` swallows
every exception and returns 0.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("ai.client")


# ── Model ids ────────────────────────────────────────────────────────────────

ANTHROPIC_MODELS = {
    "extraction":     os.environ.get("AI_MODEL_EXTRACTION",     "claude-haiku-4-5-20251001"),
    "categorisation": os.environ.get("AI_MODEL_CATEGORISATION", "claude-haiku-4-5-20251001"),
    "summarisation":  os.environ.get("AI_MODEL_SUMMARISATION",  "claude-sonnet-4-5-20250929"),
    "environmental":  os.environ.get("AI_MODEL_ENVIRONMENTAL",  "claude-sonnet-4-5-20250929"),
    "correlation":    os.environ.get("AI_MODEL_CORRELATION",    "claude-sonnet-4-5-20250929"),
    "weekly_report":  os.environ.get("AI_MODEL_WEEKLY_REPORT",  "claude-sonnet-4-5-20250929"),
}


# ── Per-token prices (USD per million tokens) ────────────────────────────────

PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001":  (0.25, 1.25),
    "claude-haiku-4-5-20250929":  (0.25, 1.25),
    "claude-haiku-4-5":           (0.25, 1.25),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-sonnet-4-5":          (3.0, 15.0),
    "claude-sonnet-4":            (3.0, 15.0),
    "claude-opus-4-7":            (15.0, 75.0),
}


def cost_for(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = PRICES.get(model) or (0.0, 0.0)
    in_rate, out_rate = rates
    return round(
        (float(input_tokens or 0) * in_rate +
         float(output_tokens or 0) * out_rate) / 1_000_000.0,
        6,
    )


# ── SDK handle ───────────────────────────────────────────────────────────────


def get_async_client() -> Any:
    """Return AsyncAnthropic or None. Tests monkey-patch this.

    Never raises — callers check for None and fall back to a stub.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK not installed")
        return None
    try:
        return anthropic.AsyncAnthropic(api_key=api_key)
    except Exception as exc:
        log.error("AsyncAnthropic instantiation failed: %s", exc)
        return None


# ── DB path resolution ───────────────────────────────────────────────────────
#
# The ai/ package is forbidden from importing db.py (see prompt: DO NOT
# edit db.py), so we resolve the path the same way db.py does — env var
# GATEWAY_DB_PATH else ./auth.db — but with our own connection so we can
# always log even if the caller's connection is already locked.


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "auth.db"


class _PassThroughConn:
    """Wraps a db.conn() context manager so the rest of ai/client can use
    `with _connect() as c: c.execute(...)` uniformly — whether we're
    delegating to the shared db.conn (tests, production) or opening our
    own sqlite file (early bootstrap, migrations).
    """

    def __init__(self, ctx):
        self._ctx = ctx
        self._conn = None

    def __enter__(self):
        self._conn = self._ctx.__enter__()
        return self._conn

    def __exit__(self, *exc):
        return self._ctx.__exit__(*exc)

    def execute(self, *args, **kwargs):
        if self._conn is None:
            raise RuntimeError("_PassThroughConn must be used as a context manager")
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        if self._conn is not None:
            self._conn.commit()

    def close(self):
        pass  # context manager handles this


def _connect():
    """Open a sqlite connection for usage logging and kill-switch reads.

    Prefers ``db.conn()`` so tests that monkey-patch it see every log
    row. Falls back to a direct sqlite3 connection if ``db`` can't be
    imported (early startup) — that path uses a raw connection you must
    .close() yourself.
    """
    try:
        import db as _db
        return _PassThroughConn(_db.conn())
    except Exception:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


# ── Usage logging ────────────────────────────────────────────────────────────


def _extract_usage(response: Any) -> tuple[int, int]:
    if response is None:
        return 0, 0
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0

    def _field(obj: Any, key: str) -> int:
        val = None
        if hasattr(obj, key):
            val = getattr(obj, key, None)
        elif isinstance(obj, dict):
            val = obj.get(key)
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0
    return _field(usage, "input_tokens"), _field(usage, "output_tokens")


def log_claude_usage_row(
    *,
    feature: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    cached_hit: bool = False,
    request_id: Optional[str] = None,
    user_id: Optional[int] = None,
) -> int:
    """Insert a row into claude_usage_log. Never raises.

    Tolerant of the older 9-column schema (pre-migration 051) — probes
    the columns at runtime and only sends the ones that exist.
    """
    try:
        with _connect() as conn:
            try:
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(claude_usage_log)")}
            except sqlite3.Error:
                return 0
            if not cols:
                return 0

            base_cols = ["timestamp", "feature", "model", "input_tokens",
                         "output_tokens", "cost_usd", "cached_hit"]
            base_vals: list[Any] = [
                int(time.time()), feature, model,
                int(input_tokens or 0), int(output_tokens or 0),
                float(cost_usd or 0.0), 1 if cached_hit else 0,
            ]
            if "request_id" in cols:
                base_cols.append("request_id")
                base_vals.append(request_id or "")
            if "user_id" in cols:
                base_cols.append("user_id")
                base_vals.append(user_id)

            placeholders = ",".join("?" * len(base_cols))
            sql = f"INSERT INTO claude_usage_log ({','.join(base_cols)}) VALUES ({placeholders})"
            cur = conn.execute(sql, tuple(base_vals))
            return cur.lastrowid or 0
    except Exception:
        log.exception("log_claude_usage_row failed")
        return 0


def log_response(
    *,
    feature: str,
    model: str,
    response: Any,
    cached_hit: bool = False,
    user_id: Optional[int] = None,
    request_id: Optional[str] = None,
) -> int:
    if request_id is None:
        request_id = uuid.uuid4().hex
    if cached_hit:
        return log_claude_usage_row(
            feature=feature, model=model,
            input_tokens=0, output_tokens=0, cost_usd=0.0,
            cached_hit=True, request_id=request_id, user_id=user_id,
        )
    it, ot = _extract_usage(response)
    return log_claude_usage_row(
        feature=feature, model=model,
        input_tokens=it, output_tokens=ot,
        cost_usd=cost_for(model, it, ot),
        cached_hit=False, request_id=request_id, user_id=user_id,
    )


def log_failure(
    *,
    feature: str,
    model: str,
    user_id: Optional[int] = None,
    request_id: Optional[str] = None,
) -> int:
    if request_id is None:
        request_id = uuid.uuid4().hex
    return log_claude_usage_row(
        feature=feature, model=model,
        input_tokens=0, output_tokens=0, cost_usd=0.0,
        cached_hit=False, request_id=request_id, user_id=user_id,
    )


# ── Kill switch ────────────────────────────────────────────────────────────
#
# When ``claude_kill_switch.active = 1`` every uncached call_claude returns
# None. Tripped by jobs/claude_cost_check.py above $200/day, or manually by
# a super-admin from /admin/ai-usage. Cache reads are NOT affected — users
# still see cached results while the switch is on.


def is_kill_switch_active() -> bool:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT active FROM claude_kill_switch WHERE id = 1"
            ).fetchone()
    except sqlite3.Error:
        return False
    if not row:
        return False
    try:
        return bool(row["active"])
    except (KeyError, IndexError):
        return False


def set_kill_switch(
    *,
    active: bool,
    reason: Optional[str] = None,
    triggered_by: Optional[str] = None,
) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE claude_kill_switch SET active = ?, reason = ?, "
                "triggered_at = ?, triggered_by = ? WHERE id = 1",
                (
                    1 if active else 0,
                    reason,
                    int(time.time()) if active else None,
                    triggered_by,
                ),
            )
    except sqlite3.Error as exc:
        log.warning("set_kill_switch failed: %s", exc)


def get_kill_switch_status() -> dict:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT active, reason, triggered_at, triggered_by "
                "FROM claude_kill_switch WHERE id = 1"
            ).fetchone()
    except sqlite3.Error:
        return {"active": False, "reason": None, "triggered_at": None, "triggered_by": None}
    if not row:
        return {"active": False, "reason": None, "triggered_at": None, "triggered_by": None}
    return {
        "active": bool(row["active"]),
        "reason": row["reason"],
        "triggered_at": row["triggered_at"],
        "triggered_by": row["triggered_by"],
    }


# ── Unified call_claude ────────────────────────────────────────────────────
#
# Every feature module should use this. The contract:
#
#   - If ``cache_key`` is set and the cache has a value, return it + log
#     a cache-hit row. Zero network traffic, zero tokens billed.
#   - Otherwise, if the kill switch is on, return None and log a failure
#     row so the dashboard shows something.
#   - Otherwise, dispatch to AsyncAnthropic, log tokens + cost, write the
#     result back to cache (if ``cache_key`` was set), return the text.
#
# Failures never raise; callers get None and handle fallback themselves —
# that matches the existing ai/* modules' defensive contract.


async def call_claude(
    *,
    feature: str,
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = 1024,
    cache_key: Optional[str] = None,
    cache_ttl_seconds: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Optional[str]:
    """Cache-aware Claude call with unified logging and kill-switch.

    Returns the response text, or None on failure / kill-switch hit with
    no cache. The feature module decides what to do with None.
    """
    mdl = model or ANTHROPIC_MODELS.get(feature) or "claude-haiku-4-5-20251001"

    if cache_key:
        from ai import cache as _cache
        cached = _cache.get(cache_key)
        if cached is not None:
            log_response(
                feature=feature, model=mdl,
                response=None, cached_hit=True, user_id=user_id,
            )
            return cached if isinstance(cached, str) else None

    if is_kill_switch_active():
        log.warning("call_claude: kill-switch active — feature=%s", feature)
        log_failure(feature=feature, model=mdl, user_id=user_id)
        return None

    sdk = get_async_client()
    if sdk is None:
        log_failure(feature=feature, model=mdl, user_id=user_id)
        return None

    try:
        resp = await sdk.messages.create(
            model=mdl,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:
        log.error("call_claude: %s failed: %s", feature, exc)
        log_failure(feature=feature, model=mdl, user_id=user_id)
        return None

    log_response(
        feature=feature, model=mdl,
        response=resp, cached_hit=False, user_id=user_id,
    )

    parts: list[str] = []
    for block in resp.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    out = "".join(parts) if parts else None

    if cache_key and out is not None and cache_ttl_seconds:
        from ai import cache as _cache
        _cache.set(
            cache_key, out,
            ttl_seconds=cache_ttl_seconds, feature=feature, model=mdl,
        )

    return out
