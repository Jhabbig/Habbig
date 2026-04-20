"""
Bluesky source — public AT Protocol `searchPosts` polling across a static term list.

No auth required for public read. We hit the unauthenticated app.bsky.app XRPC
endpoint directly. User-Agent is polite.

Why search-by-term and not a firehose: for MVP we want the same shape as Reddit
(fixed poll list, per-term isolation) so the multi-source corroboration gate
can compare apples to apples. A Jetstream firehose subscription is richer but
requires a persistent WS connection and its own dedup layer — deferred.

Mirrors sources/reddit.py:
  * Per-term exponential backoff (60s → 3600s) on 429/403 so one hot term
    doesn't stall the rest of the loop.
  * One page per term per loop (25 posts × ~18 terms = ~450 posts/cycle).
    Cursor pagination deferred — re-add if under-fetching shows up in logs.
  * All state-mutating globals (`_backoff`) isolated to this module so the
    unit tests can import and reset between runs.

Validated by DECISIONS.md #13 as the first second-source; the existing
SourceBase ABC and RawPost shape covered AT Protocol's JSON cleanly with
zero interface changes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

import config
from sources.base import SourceBase, RawPost

log = logging.getLogger("annoyance.sources.bluesky")


# Public Bluesky app-view XRPC endpoint. This is the same endpoint the web
# client uses for unauthenticated search — no access token needed.
_SEARCH_URL = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"


# Per-term exponential backoff state. Key = search term, value = (ready_at_monotonic, fail_count).
# Isolated at module level so tests can clear it between runs (see test fixtures).
_backoff: dict[str, tuple[float, int]] = {}


def _is_term_in_backoff(term: str) -> bool:
    entry = _backoff.get(term)
    if not entry:
        return False
    ready_at, _ = entry
    return time.monotonic() < ready_at


def _record_term_failure(term: str) -> None:
    _, fail_count = _backoff.get(term, (0.0, 0))
    fail_count += 1
    # 60s, 120s, 240s, 480s, … capped at 1h. Same curve as Reddit's per-sub.
    delay = min(60 * (2 ** (fail_count - 1)), 3600)
    _backoff[term] = (time.monotonic() + delay, fail_count)
    log.warning("bluesky term %r in backoff for %ds (failures=%d)", term, delay, fail_count)


def _record_term_success(term: str) -> None:
    if term in _backoff:
        del _backoff[term]


def _reset_backoff_for_tests() -> None:
    """Test helper — wipe the module-level backoff state."""
    _backoff.clear()


def _rkey_from_uri(uri: str) -> Optional[str]:
    """Bluesky post URIs look like `at://did:plc:xyz/app.bsky.feed.post/3kxyz…`.
    The rkey (record key) is the last path segment — that's what goes into the
    bsky.app web URL."""
    if not uri:
        return None
    # Split off the at:// scheme, then take the last path segment
    tail = uri.rsplit("/", 1)
    return tail[1] if len(tail) == 2 and tail[1] else None


class BlueskySource(SourceBase):
    """Poll `app.bsky.feed.searchPosts` for each configured term.

    Contract matches SourceBase — returns a flat list of RawPost dicts.
    Caller dedups via (id = "bluesky:{cid}") so repeats across loops or terms
    are harmless.
    """

    name = "bluesky"

    async def fetch(self) -> list[RawPost]:
        all_posts: list[RawPost] = []
        headers = {
            "User-Agent": config.BLUESKY_USER_AGENT,
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(
            headers=headers, timeout=15.0, follow_redirects=True,
        ) as client:
            # Track whether every term backed off — if so, sleep once at the
            # top of the next loop iteration instead of tight-looping.
            attempted = 0
            succeeded = 0

            for term in config.BLUESKY_SEARCH_TERMS:
                if _is_term_in_backoff(term):
                    continue
                attempted += 1

                try:
                    posts = await self._fetch_term(client, term)
                    all_posts.extend(posts)
                    _record_term_success(term)
                    succeeded += 1
                    log.info("bluesky %r: %d posts", term, len(posts))
                except httpx.HTTPStatusError as e:
                    status = e.response.status_code if e.response else 0
                    if status in (429, 403):
                        _record_term_failure(term)
                    else:
                        log.warning("bluesky %r HTTP %s: %s", term, status, e)
                except Exception:
                    log.exception("bluesky %r unexpected error", term)

                await asyncio.sleep(config.BLUESKY_REQUEST_SPACING_SECONDS)

            # If every term we attempted failed, the server is likely rate-
            # limiting us globally. Let the caller know via an empty return
            # (source_status ok=True; the loop-level log makes it obvious).
            if attempted > 0 and succeeded == 0:
                log.warning(
                    "bluesky: all %d attempted terms failed this cycle — likely global rate limit",
                    attempted,
                )

        return all_posts

    async def _fetch_term(self, client: httpx.AsyncClient, term: str) -> list[RawPost]:
        params = {"q": term, "limit": config.BLUESKY_POSTS_PER_TERM}
        resp = await client.get(_SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        raw_posts = data.get("posts", []) or []

        out: list[RawPost] = []
        for raw in raw_posts:
            parsed = self._parse(raw, term)
            if parsed:
                out.append(parsed)
        return out

    def _parse(self, raw: dict, term: str) -> Optional[RawPost]:
        """Convert one AT Protocol post record into our RawPost shape.

        AT Protocol post shape (trimmed to what we use):
            {
              "uri": "at://did:plc:xyz/app.bsky.feed.post/3k…",
              "cid": "bafy…",
              "author": { "handle": "someone.bsky.social", "did": "did:plc:…" },
              "record": { "text": "…", "createdAt": "2026-04-14T…Z" },
              "likeCount": 42, "repostCount": 3, "replyCount": 1
            }
        """
        cid = raw.get("cid")
        if not cid:
            return None

        author = raw.get("author") or {}
        handle = author.get("handle") or None

        record = raw.get("record") or {}
        text = (record.get("text") or "").strip()
        if not text:
            return None

        posted_at = record.get("createdAt")
        if not posted_at:
            return None

        # Build the web URL from the AT URI. Works even when author.handle is
        # missing (fall back to did — bsky.app redirects either way).
        rkey = _rkey_from_uri(raw.get("uri") or "")
        if handle and rkey:
            url = f"https://bsky.app/profile/{handle}/post/{rkey}"
        elif rkey:
            did = author.get("did") or ""
            url = f"https://bsky.app/profile/{did}/post/{rkey}" if did else None
        else:
            url = None

        # Missing engagement counters are common on brand-new posts — default
        # to 0 rather than None so downstream math stays simple.
        likes = int(raw.get("likeCount") or 0)
        reposts = int(raw.get("repostCount") or 0)
        replies = int(raw.get("replyCount") or 0)
        engagement = likes + reposts + replies

        return RawPost(
            id=f"bluesky:{cid}",
            source="bluesky",
            source_channel=f"search:{term}",
            author=handle,
            content=text[:4000],  # same cap as Reddit to guard against abuse
            posted_at=posted_at,
            url=url,
            engagement=engagement,
            keyword=term,
        )
