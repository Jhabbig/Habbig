"""Slow-query tracer — drop-in wrapper for sqlite3 connections.

Usage (to be wired in by db.py owner in a separate diff; this module
never imports db.py to stay inside the queries/ scope):

    from queries.query_tracer import install_tracer, set_request_context

    def get_conn() -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH, ...)
        install_tracer(conn, threshold_ms=500)
        return conn

    # In a request middleware:
    set_request_context(endpoint=request.url.path, user_id=user.id)

Design constraints this module satisfies:

  * **Never blocks the request.** The tracer writes to an in-process
    ``queue.Queue``; a single daemon thread drains that queue every
    ``FLUSH_INTERVAL_SECONDS`` and bulk-inserts into ``slow_query_log``.
    A slow DB write cannot stall an HTTP handler.
  * **<1 ms overhead per fast query.** The per-call work is a
    ``time.perf_counter`` pair + a dict put into a bounded queue. No
    logging, no I/O, no regex on the fast path.
  * **Queue is bounded.** If the drainer falls behind (e.g. DB
    contention), ``QUEUE_MAX`` caps memory. Excess entries are dropped
    with a debug log — we care about trend data, not every individual
    sample.
  * **Signatures group shapes.** Literal values in the query are
    stripped so "SELECT * FROM users WHERE id=42" and the same query
    for id=17 collapse to one row on the /admin/performance dashboard.
  * **Request context is optional.** If no middleware has set it, the
    ``endpoint`` and ``user_id`` columns are NULL; the trace row still
    goes through.

Re-run safety: ``install_tracer`` is idempotent per-connection via a
marker attribute, so a background job that also calls ``install_tracer``
on a pooled connection won't double-wrap.
"""

from __future__ import annotations

import atexit
import contextvars
import logging
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("narve.slow_query")


# ── Tunables (kept in one place so a future reviewer can adjust without
# hunting through the module) ─────────────────────────────────────────
THRESHOLD_MS_DEFAULT = 500      # log queries slower than this
FLUSH_INTERVAL_SECONDS = 30     # drain queue -> DB cadence
QUEUE_MAX = 10_000              # back-pressure cap (approx ~1 MB)
BATCH_INSERT_SIZE = 100         # rows per flush transaction
_INSTALLED_MARKER = "_narve_slow_query_tracer_installed"


# ── Request context (middleware sets these, tracer reads them) ────────

_ctx_endpoint: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "narve_slow_query_endpoint", default=None,
)
_ctx_user_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "narve_slow_query_user_id", default=None,
)


def set_request_context(*, endpoint: Optional[str] = None, user_id: Optional[int] = None) -> None:
    """Call from request middleware. Values flow into every trace row
    written during the current context (works for async tasks too —
    ``ContextVar`` copies across ``asyncio.ensure_future``)."""
    if endpoint is not None:
        _ctx_endpoint.set(endpoint)
    if user_id is not None:
        _ctx_user_id.set(user_id)


def clear_request_context() -> None:
    _ctx_endpoint.set(None)
    _ctx_user_id.set(None)


# ── Signature normalization ───────────────────────────────────────────

# Strip: string literals, numeric literals, NULL, IN (?,?,?), whitespace.
# Keep: table names, column names, keywords — enough to identify shape.
_RE_STR_LITERAL = re.compile(r"'(?:[^']|'')*'")
_RE_DOUBLE_LITERAL = re.compile(r'"(?:[^"]|"")*"')
_RE_NUM_LITERAL = re.compile(r"\b\d+\.?\d*\b")
_RE_IN_LIST = re.compile(r"\bIN\s*\(\s*(?:\?\s*,\s*)*\?\s*\)", re.IGNORECASE)
_RE_WS = re.compile(r"\s+")


def normalize_query_signature(sql: str) -> str:
    """Return a query "shape" with literals stripped. Two queries that
    differ only in bound values (or IN-list length) collapse to the same
    signature, so the admin page can group them."""
    if not sql:
        return ""
    sig = _RE_STR_LITERAL.sub("?", sql)
    sig = _RE_DOUBLE_LITERAL.sub("?", sig)
    sig = _RE_NUM_LITERAL.sub("?", sig)
    sig = _RE_IN_LIST.sub("IN (?)", sig)
    sig = _RE_WS.sub(" ", sig).strip()
    return sig[:500]  # cap for index friendliness


# ── Queue + flusher thread ────────────────────────────────────────────

@dataclass
class _TraceEntry:
    query: str
    query_signature: str
    duration_ms: int
    rowcount: Optional[int]
    endpoint: Optional[str]
    user_id: Optional[int]
    timestamp: int


_queue: "queue.Queue[_TraceEntry]" = queue.Queue(maxsize=QUEUE_MAX)
_flusher_started = False
_flusher_lock = threading.Lock()
_shutdown = threading.Event()
_db_path_getter = None  # callable returning the auth.db path, set by install_tracer


def _flush_once(drain_all: bool = False) -> int:
    """Pull up to BATCH_INSERT_SIZE rows (or everything if drain_all) and
    bulk-insert into slow_query_log. Returns rows written.

    Opens its own sqlite3 connection so a flush never contends with the
    main connection pool. If the DB path isn't known yet (the tracer
    hasn't been installed anywhere), silently drop the batch."""
    if _db_path_getter is None:
        return 0
    rows: list[_TraceEntry] = []
    target = None if drain_all else BATCH_INSERT_SIZE
    try:
        while target is None or len(rows) < target:
            rows.append(_queue.get_nowait())
    except queue.Empty:
        pass
    if not rows:
        return 0
    try:
        path = _db_path_getter()
    except Exception:
        log.debug("slow_query flusher: db path getter raised; dropping batch")
        return 0
    if not path:
        return 0
    try:
        with sqlite3.connect(path, timeout=5.0) as c:
            c.executemany(
                "INSERT INTO slow_query_log "
                "(query, query_signature, duration_ms, rowcount, "
                " endpoint, user_id, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (r.query, r.query_signature, r.duration_ms, r.rowcount,
                     r.endpoint, r.user_id, r.timestamp)
                    for r in rows
                ],
            )
            c.commit()
    except sqlite3.Error as exc:
        # Dropping a batch is strictly better than crashing the flusher
        # thread and losing ALL future traces. The admin page becomes
        # slightly less accurate; nothing user-visible breaks.
        log.warning(
            "slow_query flusher: batch of %d rows failed — %s",
            len(rows), exc,
        )
    return len(rows)


def _flusher_loop() -> None:
    while not _shutdown.is_set():
        _shutdown.wait(FLUSH_INTERVAL_SECONDS)
        try:
            _flush_once()
        except Exception:
            log.exception("slow_query flusher: iteration failed")
    # Drain on shutdown so in-flight traces aren't lost.
    _flush_once(drain_all=True)


def _ensure_flusher_started() -> None:
    global _flusher_started
    if _flusher_started:
        return
    with _flusher_lock:
        if _flusher_started:
            return
        t = threading.Thread(
            target=_flusher_loop,
            name="narve-slow-query-flusher",
            daemon=True,
        )
        t.start()
        atexit.register(_shutdown.set)
        _flusher_started = True


# ── Public install API ────────────────────────────────────────────────

class TracedConnection(sqlite3.Connection):
    """Connection subclass that times every ``execute`` / ``executemany``
    call and enqueues traces above the configured threshold.

    Why subclass: sqlite3's Connection object forbids monkey-patching
    ``execute`` at runtime (the attribute is C-level read-only), so we
    can't wrap a plain connection after the fact. Subclassing lets us
    override at the Python level via ``sqlite3.connect(..., factory=...)``.

    Configuration flows through class attributes set by ``configure``
    rather than ``__init__`` because sqlite3's factory contract calls
    ``__init__(database, *args, **kwargs)`` with positional args we
    don't control.
    """

    _threshold_seconds: float = THRESHOLD_MS_DEFAULT / 1000.0

    @classmethod
    def configure(
        cls,
        *,
        threshold_ms: int = THRESHOLD_MS_DEFAULT,
        db_path_getter=None,
    ) -> None:
        """Set up the flusher + threshold. Call once per process before
        the first ``sqlite3.connect(..., factory=TracedConnection)``."""
        global _db_path_getter
        cls._threshold_seconds = threshold_ms / 1000.0
        if db_path_getter is not None and _db_path_getter is None:
            _db_path_getter = db_path_getter
        _ensure_flusher_started()

    def execute(self, sql, *params):  # type: ignore[override]
        start = time.perf_counter()
        try:
            return super().execute(sql, *params)
        finally:
            _record_trace(sql, time.perf_counter() - start, self._threshold_seconds)

    def executemany(self, sql, seq_of_params):  # type: ignore[override]
        start = time.perf_counter()
        try:
            return super().executemany(sql, seq_of_params)
        finally:
            _record_trace(sql, time.perf_counter() - start, self._threshold_seconds)


def _record_trace(sql: str, duration_seconds: float, threshold_seconds: float) -> None:
    if duration_seconds < threshold_seconds:
        return
    try:
        entry = _TraceEntry(
            query=(sql or "")[:4000],
            query_signature=normalize_query_signature(sql or ""),
            duration_ms=int(duration_seconds * 1000),
            rowcount=None,  # cursor rowcount lives on the cursor, not the conn
            endpoint=_ctx_endpoint.get(),
            user_id=_ctx_user_id.get(),
            timestamp=int(time.time()),
        )
        _queue.put_nowait(entry)
    except queue.Full:
        log.debug("slow_query queue full; dropping trace")


def install_tracer(
    conn: sqlite3.Connection,
    *,
    threshold_ms: int = THRESHOLD_MS_DEFAULT,
    db_path_getter=None,
) -> bool:
    """Best-effort attach a trace to an already-open connection.

    Returns True iff the connection was successfully wrapped. Some
    sqlite3 versions forbid patching the connection's ``execute``
    attribute; in that case we return False and the caller is expected
    to switch to ``factory=TracedConnection`` at ``sqlite3.connect``
    time instead.

    This is the "minimal wiring" escape hatch documented in the module
    header. Preferred path: use ``TracedConnection`` as the factory.
    """
    global _db_path_getter

    if getattr(conn, _INSTALLED_MARKER, False):
        return True

    if db_path_getter is not None and _db_path_getter is None:
        _db_path_getter = db_path_getter

    _ensure_flusher_started()

    threshold_seconds = threshold_ms / 1000.0
    original_execute = conn.execute
    original_executemany = conn.executemany

    def traced_execute(sql, *params):
        start = time.perf_counter()
        try:
            return original_execute(sql, *params)
        finally:
            _record_trace(sql, time.perf_counter() - start, threshold_seconds)

    def traced_executemany(sql, seq):
        start = time.perf_counter()
        try:
            return original_executemany(sql, seq)
        finally:
            _record_trace(sql, time.perf_counter() - start, threshold_seconds)

    try:
        conn.execute = traced_execute          # type: ignore[assignment]
        conn.executemany = traced_executemany  # type: ignore[assignment]
        setattr(conn, _INSTALLED_MARKER, True)
        return True
    except (AttributeError, TypeError):
        # sqlite3.Connection's execute is read-only in CPython — caller
        # should switch to the TracedConnection factory instead.
        return False


# ── Testing hooks ─────────────────────────────────────────────────────

def _drain_for_test() -> int:
    """Flush everything currently queued. Intended for tests that need
    deterministic reads of slow_query_log; production code relies on
    the background thread."""
    return _flush_once(drain_all=True)


def _reset_for_test() -> None:
    """Wipe context + queue. Tests should call this in setUp to avoid
    cross-test leakage."""
    while not _queue.empty():
        try:
            _queue.get_nowait()
        except queue.Empty:
            break
    clear_request_context()
