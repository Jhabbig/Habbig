"""
Receiver module — handles on-demand pull requests from the main server.

When the main server calls POST /pull on the scraper API, the scraper
runs an immediate scrape and makes posts available for pickup via
GET /posts/untransmitted + POST /posts/acknowledge.

This module manages pull job state.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("scraper")


class PullJob:
    """Tracks state of an on-demand pull job."""

    def __init__(self, platform: str, keywords: list[str] | None = None):
        self.job_id = str(uuid.uuid4())[:12]
        self.platform = platform
        self.keywords = keywords
        self.status: str = "running"  # running | complete | failed
        self.posts_found: int = 0
        self.error: str | None = None
        self.created_at: datetime = datetime.now(timezone.utc)
        self.completed_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "platform": self.platform,
            "keywords": self.keywords,
            "status": self.status,
            "posts_found": self.posts_found,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class PullJobManager:
    """In-memory store for active pull jobs."""

    def __init__(self):
        self._jobs: dict[str, PullJob] = {}

    def create(self, platform: str, keywords: list[str] | None = None) -> PullJob:
        job = PullJob(platform, keywords)
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> PullJob | None:
        return self._jobs.get(job_id)

    def complete(self, job_id: str, posts_found: int) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.status = "complete"
            job.posts_found = posts_found
            job.completed_at = datetime.now(timezone.utc)

    def fail(self, job_id: str, error: str) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.status = "failed"
            job.error = error
            job.completed_at = datetime.now(timezone.utc)

    def cleanup_old(self, max_age_minutes: int = 60) -> None:
        """Remove completed/failed jobs older than max_age_minutes."""
        now = datetime.now(timezone.utc)
        to_remove = []
        for jid, job in self._jobs.items():
            if job.completed_at:
                age = (now - job.completed_at).total_seconds() / 60
                if age > max_age_minutes:
                    to_remove.append(jid)
        for jid in to_remove:
            del self._jobs[jid]


# Global instance
pull_jobs = PullJobManager()
