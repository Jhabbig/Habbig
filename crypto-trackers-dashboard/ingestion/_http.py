"""Shared HTTP helper - polite UA, sane timeout, swallow errors as None.

Records every call into ``_health`` so the dashboard can show per-source
latency badges (which is critical for a crypto-tracker product where speed
*is* the value).
"""
from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urlsplit

import requests

from . import _health

log = logging.getLogger("ct.http")

USER_AGENT = "narve-crypto-trackers/1.0 (+https://trackers.narve.ai)"


def get(url: str, *, timeout: int = 15, params: Optional[dict] = None,
        headers: Optional[dict] = None) -> Optional[requests.Response]:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    source = urlsplit(url).netloc or url
    started = time.time()
    try:
        r = requests.get(url, params=params, timeout=timeout, headers=h)
    except requests.RequestException as e:
        log.warning("HTTP error for %s: %s", url, e)
        _health.record_call(source, ok=False, latency_s=time.time() - started)
        return None
    latency = time.time() - started
    if r.status_code != 200:
        log.warning("HTTP %d for %s", r.status_code, url)
        _health.record_call(source, ok=False, latency_s=latency, http_status=r.status_code)
        return None
    _health.record_call(source, ok=True, latency_s=latency, http_status=r.status_code)
    return r
