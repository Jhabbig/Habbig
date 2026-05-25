"""HuggingFace Open LLM Leaderboard v2 ingestor.

Pulls the latest leaderboard contents via the HuggingFace datasets API. The
v2 board lives at `open-llm-leaderboard/contents` and exposes parquet files
through HF's auto-converted parquet API, which we hit as JSON via the
`/api/datasets/<id>/parquet/...` route is binary; for a no-deps client we use
the `datasets-server` rows endpoint instead, which streams JSON.

Endpoint shape:
  https://datasets-server.huggingface.co/rows
    ?dataset=open-llm-leaderboard%2Fcontents
    &config=default&split=train&offset=0&length=100

Maps the `MMLU-PRO` column (and friends) to our `mmlu_pro` benchmark key.
We pull just the top of the board (length=100) — enough to cover every
open-weight model relevant to the dashboard.
"""

from __future__ import annotations

import json
import urllib.parse

from ._common import TTLCache, http_get

SOURCE_KEY = "openllm"
SOURCE_NAME = "HuggingFace Open LLM Leaderboard v2"
BENCHMARK_KEY = "mmlu_pro"  # this ingestor's primary surface; we also emit gpqa.
URL_DOC = "https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard"

DATASET = "open-llm-leaderboard/contents"
ROWS_BASE = "https://datasets-server.huggingface.co/rows"

# Column-name aliases on the HF board → our benchmark keys. We accept any of
# these; first match wins. (HF has renamed columns across versions.)
COLUMN_MAP = {
    "mmlu_pro": ["MMLU-PRO", "MMLU_PRO", "mmlu_pro", "MMLU-Pro"],
    "gpqa_diamond": ["GPQA", "GPQA-Diamond", "gpqa", "gpqa_diamond"],
}
NAME_COLS = ["fullname", "Model", "model", "eval_name"]

_TTL = 60 * 60  # 1 hour
_cache = TTLCache(_TTL)


def _build_url() -> str:
    qs = urllib.parse.urlencode({
        "dataset": DATASET,
        "config": "default",
        "split": "train",
        "offset": 0,
        "length": 100,
    })
    return f"{ROWS_BASE}?{qs}"


def _pick(row: dict, candidates: list[str]):
    for c in candidates:
        if c in row and row[c] not in (None, "", "NaN"):
            return row[c]
    return None


def _fetch() -> dict:
    body = http_get(_build_url())
    payload = json.loads(body)
    rows = payload.get("rows") or []
    entries: list[dict] = []
    for r in rows:
        row = r.get("row") or {}
        name = _pick(row, NAME_COLS)
        if not name:
            continue
        # Emit one entry per benchmark we can find. The merge layer routes
        # each entry to its target benchmark via the `benchmark` field.
        for bench_key, cols in COLUMN_MAP.items():
            v = _pick(row, cols)
            if v is None:
                continue
            try:
                score = float(v)
            except (TypeError, ValueError):
                continue
            entries.append({"model": str(name), "score": score, "benchmark": bench_key})
    return {
        "source": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "source_url": URL_DOC,
        "benchmark": BENCHMARK_KEY,
        "entries": entries,
    }


def get_cached(force: bool = False) -> dict:
    base = {
        "source": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "source_url": URL_DOC,
        "benchmark": BENCHMARK_KEY,
    }
    payload = _cache.get(_fetch, force=force)
    return {**base, **payload}
