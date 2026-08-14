"""LMArena (Chatbot Arena) Elo leaderboard ingestor.

The LMArena project publishes their leaderboard data through a HuggingFace
Space. The Space is the most stable surface: HF resolves files at predictable
paths under `huggingface.co/spaces/<owner>/<space>/resolve/main/<path>`.

Their CSV format and exact filename have evolved across versions. To absorb
that, we try a small list of candidate URLs in order and parse the first one
that returns. If all fail we report `ok: false` with the error chain — the
operator can update LMARENA_URLS to point at the current artifact.

Schema we accept: a CSV with a `Model` (or `model`) column and an
`Arena Elo` / `Elo` / `arena_elo` / `Score` column. Header detection is
case-insensitive and substring-tolerant.
"""

from __future__ import annotations

import csv
import io

from ._common import TTLCache, http_get

SOURCE_KEY = "lmarena"
SOURCE_NAME = "LMArena (Chatbot Arena)"
BENCHMARK_KEY = "lmarena_elo"
URL_DOC = "https://lmarena.ai/leaderboard"

# Candidate URLs, tried in order. Update these when LMArena changes their
# publication layout — the rest of the parser auto-adapts.
LMARENA_URLS = [
    "https://huggingface.co/spaces/lmarena-ai/chatbot-arena-leaderboard/resolve/main/leaderboard.csv",
    "https://huggingface.co/spaces/lmarena-ai/chatbot-arena-leaderboard/resolve/main/leaderboard_v2.csv",
    "https://storage.googleapis.com/arena_external_data/public/leaderboard.csv",
]

_TTL = 60 * 60  # 1 hour — Elo board updates a few times a day at most.
_cache = TTLCache(_TTL)


def _find_col(header: list[str], *candidates: str) -> int | None:
    norm = [h.strip().lower() for h in header]
    for c in candidates:
        c = c.lower()
        for i, h in enumerate(norm):
            if c == h or c in h:
                return i
    return None


def _parse_csv(body: bytes) -> list[dict]:
    text = body.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return []
    header, *data = rows
    name_idx = _find_col(header, "model", "name")
    elo_idx = _find_col(header, "arena elo", "arena_elo", "elo", "score", "rating")
    if name_idx is None or elo_idx is None:
        raise ValueError(f"unexpected header: {header}")
    out: list[dict] = []
    for r in data:
        if len(r) <= max(name_idx, elo_idx):
            continue
        name = (r[name_idx] or "").strip()
        try:
            score = float((r[elo_idx] or "").strip())
        except ValueError:
            continue
        if not name:
            continue
        out.append({"model": name, "score": score})
    return out


def _fetch() -> dict:
    last_err: Exception | None = None
    for url in LMARENA_URLS:
        try:
            body = http_get(url)
            entries = _parse_csv(body)
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
    raise RuntimeError(f"all LMArena URLs failed; last error: {last_err}")


def get_cached(force: bool = False) -> dict:
    base = {
        "source": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "source_url": URL_DOC,
        "benchmark": BENCHMARK_KEY,
    }
    payload = _cache.get(_fetch, force=force)
    return {**base, **payload}
