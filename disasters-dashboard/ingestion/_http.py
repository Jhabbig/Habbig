"""Shared HTTP helper - polite UA, sane timeout, swallow errors as None."""
from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger("disasters.http")

USER_AGENT = "narve-disasters-dashboard/1.0 (+https://disasters.narve.ai)"


def get(url: str, *, timeout: int = 20, params: Optional[dict] = None,
        headers: Optional[dict] = None) -> Optional[requests.Response]:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    try:
        r = requests.get(url, params=params, timeout=timeout, headers=h)
    except requests.RequestException as e:
        log.warning("HTTP error for %s: %s", url, e)
        return None
    if r.status_code != 200:
        log.warning("HTTP %d for %s", r.status_code, url)
        return None
    return r
