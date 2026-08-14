"""Shared HTTP fetch helper.

A thin wrapper around `requests.get` that pins our user-agent (so upstream
operators can identify us) and never raises — fetchers expect None on failure
and cache last-good responses anyway.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger("climate.http")

USER_AGENT = "polymarket-climate-dashboard/1.0 (+https://climate.narve.ai)"


def get(url: str, *, timeout: int = 20, params: Optional[dict] = None) -> Optional[requests.Response]:
    try:
        r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT})
        if r.status_code == 200:
            return r
        logger.warning("HTTP %d for %s", r.status_code, url)
        return None
    except Exception as e:
        logger.warning("HTTP error for %s: %s", url, e)
        return None
