"""Background jobs for GDPR data exports.

Two jobs:
  * generate_data_export(export_id) — build a single user's ZIP
  * cleanup_expired_data_exports() — sweep TTL'd files (cron)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.exports")


@register_job("generate_data_export")
async def generate_data_export(export_id: int) -> dict[str, Any]:
    """ARQ entry point — defers to exports.generate."""
    from exports import generate

    # exports.generate is sync (zip building is CPU-bound stdlib work).
    # Run it inline; the worker can take the few seconds.
    return generate(export_id)


@register_job("cleanup_expired_data_exports")
async def cleanup_expired_data_exports() -> dict[str, Any]:
    """Mark ready exports past expires_at as 'expired' and unlink the files."""
    import db

    expired = db.expire_old_exports()
    removed = 0
    for row in expired:
        path = row["file_path"]
        if path:
            try:
                os.unlink(path)
                removed += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("cleanup_expired_data_exports: %s: %s", path, e)
    return {"expired": len(expired), "files_removed": removed}


# Sweep daily at 03:30 UTC — well after the morning briefing window so we
# don't compete with the email send burst.
register_cron("cleanup_expired_data_exports", hour=3, minute=30)
