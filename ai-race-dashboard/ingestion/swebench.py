"""SWE-bench Verified leaderboard ingestor.

The SWE-bench team publishes their leaderboard on swebench.com. The site is
a static-built React app; the underlying data is shipped as a JSON blob in
the page bundle and also lives in their public GitHub repo at:

    github.com/SWE-bench/experiments/blob/main/evaluation/verified/<entry>.json

For this ingestor we hit the maintainer-curated leaderboard summary file
that aggregates all entries — we try a couple of candidate paths and parse
whichever resolves first. If all fail (e.g. they restructure the repo), we
report `ok: false` and the dashboard falls back to curated values.
"""

from __future__ import annotations

import json

from ._common import TTLCache, http_get

SOURCE_KEY = "swebench"
SOURCE_NAME = "SWE-bench Verified"
BENCHMARK_KEY = "swe_bench_verified"
URL_DOC = "https://www.swebench.com/"

# Try these URLs in order. Each is a JSON file with shape:
#   [{"name": "...", "verified": {"resolved": <pct float>}}, ...]
# or the older flat shape:
#   [{"model": "...", "score": <pct float>}, ...]
SWEBENCH_URLS = [
    "https://raw.githubusercontent.com/SWE-bench/experiments/main/evaluation/verified/leaderboard.json",
    "https://raw.githubusercontent.com/SWE-bench/swe-bench.github.io/main/data/leaderboard_verified.json",
]

_TTL = 60 * 60 * 6  # 6 hours — board updates infrequently.
_cache = TTLCache(_TTL)


def _normalize(rows) -> list[dict]:
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        name = r.get("name") or r.get("model") or r.get("system")
        if not name:
            continue
        # Modern shape:  {"verified": {"resolved": 71.7}, ...}
        v = r.get("verified") or {}
        score = None
        for key in ("resolved", "score", "pct_resolved"):
            if key in v:
                score = v[key]
                break
        # Legacy flat shape
        if score is None:
            for key in ("verified_resolved", "score", "verified_pct"):
                if key in r:
                    score = r[key]
                    break
        if score is None:
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        # The repo sometimes stores 0–1 fractions, sometimes 0–100 percents.
        if 0 <= score <= 1:
            score *= 100
        out.append({"model": str(name), "score": round(score, 2)})
    return out


def _fetch() -> dict:
    last_err: Exception | None = None
    for url in SWEBENCH_URLS:
        try:
            body = http_get(url)
            data = json.loads(body)
            entries = _normalize(data)
            if entries:
                return {
                    "source": SOURCE_KEY,
                    "source_name": SOURCE_NAME,
                    "source_url": URL_DOC,
                    "benchmark": BENCHMARK_KEY,
                    "tried_url": url,
                    "entries": entries,
                }
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"all SWE-bench URLs failed; last error: {last_err}")


def get_cached(force: bool = False) -> dict:
    base = {
        "source": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "source_url": URL_DOC,
        "benchmark": BENCHMARK_KEY,
    }
    payload = _cache.get(_fetch, force=force)
    return {**base, **payload}
