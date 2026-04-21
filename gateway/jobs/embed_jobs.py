"""Embed widget background jobs — keep impression counting off the hot path.

``/embed/{widget_id}`` renders inside a partner's iframe and should not
wait on a SQLite write to serve its HTML. The handler enqueues
``increment_embed_impression`` for us to bump the counter async.

Dropping a queued job (rare — the in-process queue is lossy on crash)
causes an undercount, never a doublecount. Good enough for a vanity
metric; trade exactness for page-latency.
"""

from __future__ import annotations

import logging
from typing import Any

from jobs.registry import register_job


log = logging.getLogger("jobs.embed")


@register_job("increment_embed_impression")
async def increment_embed_impression(widget_id: str) -> dict[str, Any]:
    """Increment impressions + last_used_at for one widget."""
    import db
    db.increment_embed_widget_impression(widget_id)
    return {"widget_id": widget_id, "incremented": True}
