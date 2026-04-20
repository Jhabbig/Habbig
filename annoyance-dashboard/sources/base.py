"""
Base contract for data sources.

Each source subclass is an async fetcher that returns a list of dicts matching
the `RawPost` shape below. The caller (scheduler loop) is responsible for:
  1. Calling fetch()
  2. Passing each post to db.insert_post()  (dedup via PK)
  3. Recording source health via db.upsert_source_status()

Sources MUST guarantee their PK is stable and unique across reruns so the
caller's INSERT OR IGNORE deduplication works. Convention: f"{source_name}:{id}".

Interface validation
--------------------
Reddit was the first concrete implementation. Bluesky (added per DECISIONS.md
#13 as the corroborating second source) was onboarded in April 2026 without
extending this ABC — the `async fetch() -> list[RawPost]` contract and the
RawPost TypedDict covered AT Protocol's JSON response shape cleanly:

  * AT Protocol `searchPosts` returns a flat list — one page per fetch is
    enough for MVP corroboration, so no async-iterator extension needed.
  * `cid` (content identifier) makes a stable PK for the f"{source}:{id}"
    convention.
  * `record.text`, `record.createdAt`, `author.handle`, engagement counts all
    map 1:1 onto the existing RawPost fields.

Future sources that need cursor-based pagination (e.g. Jetstream firehose)
should add an optional `async def fetch_paginated() -> AsyncIterator[RawPost]`
method rather than overloading fetch(). Keep fetch() as the one-page default
so every source stays trivially callable from a simple poll loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypedDict, Optional


class RawPost(TypedDict, total=False):
    id: str               # REQUIRED. Format: "{source}:{native_id}"
    source: str           # REQUIRED. e.g. "reddit"
    source_channel: Optional[str]  # e.g. "r/mildlyinfuriating"
    author: Optional[str]
    content: str          # REQUIRED
    posted_at: str        # REQUIRED. ISO 8601
    url: Optional[str]
    engagement: int       # normalized score (ups + comments, likes+replies, etc.)
    keyword: Optional[str]


class SourceBase(ABC):
    name: str = ""  # override in subclass

    @abstractmethod
    async def fetch(self) -> list[RawPost]:
        """Pull new posts. Caller dedups via PK; return duplicates freely."""
        ...

    def is_available(self) -> bool:
        """True if this source can run right now (credentials, env vars, etc.)."""
        return True
