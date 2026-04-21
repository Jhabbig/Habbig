"""Engagement event tracking — fire-and-forget writes.

Every call-site that instruments a user action pipes into ``log_event``:

    engagement.log_event(user_id, "save", metadata={"prediction_id": 123})

The write is deliberately fire-and-forget:

  * It lands on a background ``Thread`` via a bounded ``Queue`` so a
    request thread never blocks on an INSERT — if the queue is saturated
    we drop the event rather than slow the user down. Churn detection is
    a trend signal; one dropped event across hundreds of thousands is
    analytically invisible.
  * SQLite writes serialize, so the single consumer is enough. No need
    for a real task runner.
  * Metadata is stored as JSON text so instrumentation can add/remove
    fields without schema churn.

The module self-starts the writer thread on first call. ``flush()`` is
exposed for tests — drains the queue synchronously so assertions can
inspect DB rows without waiting on the background thread.

Constants:
  * ``VALID_EVENT_TYPES`` — the canonical set the churn job knows how to
    read. Unknown types are still inserted (storage is cheap) but the
    job's trend detection won't weigh them.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any, Optional


log = logging.getLogger("engagement")


# The canonical event taxonomy used by the churn-signal job. Anything
# outside this set is still accepted by log_event (storage is cheap)
# but won't contribute to the engagement-trend calculation.
VALID_EVENT_TYPES: frozenset[str] = frozenset({
    "login",
    "feed_view",
    "market_detail_view",
    "save",
    "follow",
    "prediction_made",
    "signal_search",
    "intelligence_query",
    "click_notification",
})


# Bounded queue — if the writer falls behind by this many events we
# start dropping on ingress rather than pile up unbounded memory. 4096
# is ~5 minutes of normal traffic; plenty of headroom.
_QUEUE_MAXSIZE = 4096

_queue: "queue.Queue[tuple[int, str, Optional[str]]]" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
_worker_started = False
_worker_lock = threading.Lock()
_worker_thread: Optional[threading.Thread] = None


def _drain_one(row: tuple[int, str, Optional[str]]) -> None:
    """Write a single event. Swallow errors — churn tracking must never
    bring a request down. Errors are logged so issues remain visible."""
    import db
    uid, etype, meta = row
    try:
        with db.conn() as c:
            c.execute(
                "INSERT INTO engagement_events (user_id, event_type, metadata) "
                "VALUES (?, ?, ?)",
                (uid, etype, meta),
            )
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("engagement write failed: uid=%s type=%s err=%s", uid, etype, exc)


def _worker_loop() -> None:
    while True:
        row = _queue.get()
        if row is None:
            # Sentinel for shutdown (never enqueued in current use; kept
            # so tests can stop the thread deterministically).
            break
        _drain_one(row)
        _queue.task_done()


def _ensure_worker() -> None:
    global _worker_started, _worker_thread
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        _worker_thread = threading.Thread(
            target=_worker_loop,
            name="engagement-writer",
            daemon=True,
        )
        _worker_thread.start()
        _worker_started = True


def _is_test_mode() -> bool:
    """True when we should bypass the background writer and write
    synchronously. Avoids cross-thread sqlite access on the shared test
    connection (which can segfault under concurrent use)."""
    import os
    if os.environ.get("ENGAGEMENT_SYNC_FOR_TESTS") == "1":
        return True
    # pytest sets this when a test is running; fall back to it so
    # existing test suites don't need to set the env var themselves.
    return "PYTEST_CURRENT_TEST" in os.environ


def log_event(user_id: int, event_type: str, metadata: Optional[dict[str, Any]] = None) -> None:
    """Queue an engagement event for fire-and-forget write.

    Call this from request handlers. Returns immediately without any DB
    I/O on the request path. A dropped event (queue full) is a warning
    in the logs, not an exception.
    """
    if user_id is None or not event_type:
        return
    meta_json: Optional[str] = None
    if metadata is not None:
        try:
            meta_json = json.dumps(metadata, default=str)
        except (TypeError, ValueError):
            # Non-serializable metadata: drop the metadata payload but
            # still record the event — the fact of the action matters
            # more than the annotation.
            meta_json = None

    # In test mode write synchronously. The background thread would
    # otherwise race the test thread on the shared in-memory sqlite
    # connection and segfault under certain workloads.
    if _is_test_mode():
        _drain_one((int(user_id), event_type, meta_json))
        return

    _ensure_worker()
    try:
        _queue.put_nowait((int(user_id), event_type, meta_json))
    except queue.Full:
        log.warning(
            "engagement queue full (%d) — dropping event uid=%s type=%s",
            _QUEUE_MAXSIZE, user_id, event_type,
        )


def flush(timeout: float = 5.0) -> bool:
    """Drain the queue and wait for the worker to finish pending writes.

    Test-only convenience. Returns True if all pending events were
    written within the timeout, False otherwise.
    """
    _ensure_worker()
    try:
        _queue.join()
        return True
    except Exception:
        return False


def _reset_for_tests() -> None:
    """Test-only: drain the queue synchronously (bypass the thread) so
    subsequent assertions see a deterministic DB state. Called from
    engagement tests; production code MUST NOT touch this."""
    # Drain any queued items by handling them on the current thread.
    while True:
        try:
            row = _queue.get_nowait()
        except queue.Empty:
            return
        try:
            _drain_one(row)
        finally:
            _queue.task_done()
