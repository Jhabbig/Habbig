"""Shared httpx client + small helpers used by every scraper."""

from __future__ import annotations

import os
import httpx

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

TIMEOUT = float(os.environ.get("CULTURE_HTTP_TIMEOUT", "15"))


def client(**kwargs) -> httpx.AsyncClient:
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    headers.update(kwargs.pop("headers", {}))
    return httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=True, **kwargs)
