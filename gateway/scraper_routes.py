"""Server-side ingest endpoint for the standalone scraper service.

The scraper (separate process, ``gateway/scraper/``) scrapes Twitter /
TruthSocial and pushes batches of raw posts here. See
``scraper/transmission/pusher.py:push_batch`` for the exact wire format:

    POST /api/scraper/ingest
    Authorization: Bearer <SCRAPER_API_KEY>
    {
      "posts": [ {RawPost.to_dict()}, ... ],
      "scraper_run_id": <int|null>,
      "platform": "twitter" | "truthsocial" | "unknown"
    }

Each RawPost dict carries: id, platform, author_handle, author_display_name,
author_followers, author_verified, content, posted_at, scraped_at, likes,
retweets_or_boosts, replies, keyword_matched, transmitted,
transmission_attempts, last_transmission_attempt
(see scraper/storage/models.py:RawPost).

Auth: a shared bearer token (``SCRAPER_API_KEY``), NOT the narve session
cookie — the scraper is a headless service. The path is already in the
CSRF-exempt list (security/csrf.py), so no CSRF token is required.

This route is registered by ``server.py`` calling ``register(app)`` (the
integrator wires that in — this module does not touch server.py).
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("scraper.routes")


def _expected_api_key() -> str:
    """Resolve the shared scraper key the same way the scraper client does.

    Source of truth is the ``SCRAPER_API_KEY`` env var (scraper/config.py
    reads the identically-named var on the client side). Returns "" when
    unset — the handler treats that as "ingest disabled" and rejects 401,
    so a misconfigured server never silently accepts unauthenticated posts.
    """
    return os.environ.get("SCRAPER_API_KEY", "").strip()


def _check_auth(request: Request) -> None:
    """Validate the Authorization: Bearer <SCRAPER_API_KEY> header.

    Raises HTTPException(401) on a missing/malformed header, a missing
    server-side key, or a mismatch. Uses a constant-time compare so the
    endpoint doesn't leak the key length/prefix via timing.
    """
    expected = _expected_api_key()
    if not expected:
        log.error(
            "SCRAPER_API_KEY is not set on the server — rejecting ingest. "
            "Set the env var to enable the scraper ingest endpoint."
        )
        raise HTTPException(status_code=401, detail="Scraper ingest not configured")

    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    presented = header[len("Bearer ") :].strip()
    if not presented or not hmac.compare_digest(presented, expected):
        log.warning("Scraper ingest rejected: SCRAPER_API_KEY mismatch")
        raise HTTPException(status_code=401, detail="Invalid scraper API key")


def register(app) -> None:
    @app.post("/api/scraper/ingest", include_in_schema=False)
    async def scraper_ingest(request: Request):
        """Accept a batch of scraped posts and run them through extraction.

        Returns 200 with ``{ok, received, inserted, skipped,
        predictions_written, errors, run_id, platform}``. The scraper
        client (pusher.py) only checks for HTTP 200 to mark a batch
        transmitted; the body is informational.
        """
        _check_auth(request)

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")

        posts = payload.get("posts")
        if posts is None:
            posts = []
        if not isinstance(posts, list):
            raise HTTPException(status_code=400, detail="'posts' must be a list")

        run_id = payload.get("scraper_run_id")
        platform = payload.get("platform") or "unknown"

        # Run extraction inline. process_post is async (Claude-backed with a
        # regex fallback); ingest_and_extract swallows per-post failures so
        # one bad post can't fail the whole batch / trigger a client retry.
        from queries.scraper_ingest import ingest_and_extract

        try:
            result = await ingest_and_extract(posts)
        except Exception:
            log.exception("scraper ingest batch failed (run_id=%s)", run_id)
            # 500 so the scraper retries (pusher.py retries on 5xx).
            raise HTTPException(status_code=500, detail="Ingest failed")

        log.info(
            "scraper ingest run_id=%s platform=%s received=%d inserted=%d "
            "skipped=%d predictions=%d errors=%d",
            run_id,
            platform,
            len(posts),
            result.get("inserted", 0),
            result.get("skipped", 0),
            result.get("predictions_written", 0),
            result.get("errors", 0),
        )

        return JSONResponse(
            {
                "ok": True,
                "received": len(posts),
                "inserted": result.get("inserted", 0),
                "skipped": result.get("skipped", 0),
                "predictions_written": result.get("predictions_written", 0),
                "errors": result.get("errors", 0),
                "run_id": run_id,
                "platform": platform,
            }
        )
