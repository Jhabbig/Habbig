"""Merge curated `data.MODELS` with live scraped scores.

This module is the single source of truth for what the API serves. The
strategy:

1. Start from the curated MODELS list (provenance: `curated`, `as_of`).
2. For each ingestion source, try to match each scraped row to a curated
   model by normalized-name. If matched, override the score and tag it
   (provenance: `live`, source key, fetched_at).
3. Compute per-cell freshness so the UI can color stale cells.

Name matching is intentionally permissive — public leaderboards use
inconsistent identifiers (`gpt-5-2025-08-07`, `claude-opus-4.5-20250930`,
`Llama-4-Maverick-FP8`). We normalize aggressively then check substring
containment in either direction.
"""

from __future__ import annotations

import re
import time

import data as ai_data
from ingestion import ALL_SOURCES

# ── Name normalization ──────────────────────────────────────────────────────

_DATE_RE = re.compile(r"-?\d{4}[-_]?\d{2}[-_]?\d{2}")
_VERSION_TAIL_RE = re.compile(r"-(preview|exp|alpha|beta|rc\d*|fp\d+|int\d+|q\d+_?k?_?[a-z]*)$")


def normalize(name: str) -> str:
    """Lowercase, strip dates and quant/version tails, collapse separators."""
    s = (name or "").lower().strip()
    s = _DATE_RE.sub("", s)
    # Repeatedly strip trailing version/quant tags
    for _ in range(3):
        new = _VERSION_TAIL_RE.sub("", s)
        if new == s:
            break
        s = new
    s = re.sub(r"[\s_./]+", "-", s)        # unify separators
    s = re.sub(r"-+", "-", s).strip("-")
    return s


# Common aliases — when a curated name and the leaderboard's canonical id
# are too different for normalize() to bridge alone.
ALIASES = {
    "claude-opus-4.5": ["claude-opus-4-5", "claude-opus-4.5"],
    "claude-sonnet-4.5": ["claude-sonnet-4-5", "claude-sonnet-4.5"],
    "gemini-3-pro": ["gemini-3-pro-preview", "gemini-3-pro"],
    "gemini-2.5-pro": ["gemini-2-5-pro", "gemini-2.5-pro"],
    "gpt-5": ["gpt-5", "gpt5"],
    "openai-o3": ["o3", "openai-o3"],
    "grok-4": ["grok-4"],
    "deepseek-v3.1": ["deepseek-v3-1", "deepseek-v3.1"],
    "deepseek-r1": ["deepseek-r1"],
    "llama-4-maverick": ["llama-4-maverick", "meta-llama-4-maverick"],
    "qwen-3-max": ["qwen-3-max", "qwen3-max"],
    "mistral-large-3": ["mistral-large-3", "mistral-large-2411"],
}


def name_keys(name: str) -> set[str]:
    """All normalized identifiers a name could match against."""
    n = normalize(name)
    keys = {n}
    for canon, alist in ALIASES.items():
        canon_n = normalize(canon)
        norm_aliases = {normalize(a) for a in alist}
        if n == canon_n or n in norm_aliases:
            keys.add(canon_n)
            keys.update(norm_aliases)
    return keys


def match_model(scraped_name: str, curated_models: list[dict]) -> dict | None:
    """Find the curated model that best matches a scraped leaderboard name."""
    s_keys = name_keys(scraped_name)
    s_norm = normalize(scraped_name)

    # 1. Exact key intersection
    for m in curated_models:
        if name_keys(m["name"]) & s_keys:
            return m

    # 2. Substring (longest curated name that fits inside scraped name, or vice versa)
    best: tuple[int, dict] | None = None
    for m in curated_models:
        m_norm = normalize(m["name"])
        if not m_norm:
            continue
        if m_norm in s_norm or s_norm in m_norm:
            score = len(m_norm)
            if best is None or score > best[0]:
                best = (score, m)
    return best[1] if best else None


# ── Merge ────────────────────────────────────────────────────────────────────

# How old can a curated `as_of` be (in days) before we mark cells stale?
STALE_DAYS_THRESHOLD = 60


def _months_between(a_iso_ym: str, b_iso_ym: str) -> int:
    try:
        ay, am = (int(x) for x in a_iso_ym.split("-")[:2])
        by, bm = (int(x) for x in b_iso_ym.split("-")[:2])
        return abs((ay - by) * 12 + (am - bm))
    except Exception:
        return 0


def merged_models() -> dict:
    """Return MODELS with every score cell tagged with provenance + freshness.

    Output shape per model:
        {
          ...curated row...,
          "scores": {bench_key: float|None, ...},
          "score_meta": {
            bench_key: {
              "source": "curated"|"live:<source_key>",
              "as_of": "YYYY-MM" or unix-seconds-iso,
              "stale": bool,
            }
          }
        }
    """
    today_ym = time.strftime("%Y-%m")
    curated_as_of = ai_data.DATASET_AS_OF

    # Index each source's entries by normalized name.
    by_source: dict[str, dict] = {}
    for src in ALL_SOURCES:
        st = src.get_cached(force=False)
        by_source[src.SOURCE_KEY] = {
            "fetched_at": st.get("fetched_at", 0),
            "ok": st.get("ok", False),
            "default_bench": st.get("benchmark"),
            "entries": st.get("entries", []),
        }

    rows: list[dict] = []
    for m in ai_data.MODELS:
        new_scores = dict(m.get("scores") or {})
        meta: dict[str, dict] = {}
        row_as_of = m.get("as_of") or curated_as_of
        stale_months = _months_between(today_ym, row_as_of)
        is_stale = stale_months * 30 > STALE_DAYS_THRESHOLD
        for bench_key, score in new_scores.items():
            meta[bench_key] = {
                "source": "curated",
                "as_of": row_as_of,
                "stale": is_stale,
            } if score is not None else {
                "source": "missing",
                "as_of": None,
                "stale": True,
            }

        # Apply each ingestor.
        for source_key, payload in by_source.items():
            if not payload["ok"]:
                continue
            for entry in payload["entries"]:
                # Each entry can override its declared benchmark, otherwise
                # falls back to the source's default benchmark.
                bench_key = entry.get("benchmark") or payload["default_bench"]
                if bench_key not in new_scores and bench_key not in (b["key"] for b in ai_data.BENCHMARKS):
                    continue
                if match_model(entry["model"], [m]) is None:
                    continue
                new_scores[bench_key] = entry["score"]
                meta[bench_key] = {
                    "source": f"live:{source_key}",
                    "as_of": payload["fetched_at"],
                    "stale": False,
                    "scraped_name": entry["model"],
                }

        lab = ai_data.lab_by_key(m["lab_key"]) or {}
        rows.append({
            **m,
            "scores": new_scores,
            "score_meta": meta,
            "lab_name": lab.get("name", m["lab_key"]),
            "lab_color": lab.get("color", "#888"),
        })
    return {
        "models": rows,
        "benchmarks": ai_data.BENCHMARKS,
        "as_of": ai_data.DATASET_AS_OF,
        "sources": [
            {
                "key": k,
                "name": next((s.SOURCE_NAME for s in ALL_SOURCES if s.SOURCE_KEY == k), k),
                "ok": v["ok"],
                "fetched_at": v["fetched_at"],
                "entries": len(v["entries"]),
            }
            for k, v in by_source.items()
        ],
    }


def merged_frontier() -> dict:
    """Recompute frontier series from merged scores (live values bump curves)."""
    merged = merged_models()
    series: dict[str, list[dict]] = {b["key"]: [] for b in ai_data.BENCHMARKS}
    by_release = sorted(merged["models"], key=lambda m: m.get("released") or "")
    running: dict[str, float] = {}
    for m in by_release:
        for bench_key, score in (m.get("scores") or {}).items():
            if score is None:
                continue
            if score > running.get(bench_key, -1):
                running[bench_key] = score
                series.setdefault(bench_key, []).append({
                    "released": m["released"],
                    "model": m["name"],
                    "lab_key": m["lab_key"],
                    "score": score,
                    "source": m["score_meta"].get(bench_key, {}).get("source", "curated"),
                })
    return {"series": series, "as_of": ai_data.DATASET_AS_OF}


def sources_status() -> list[dict]:
    out = []
    for src in ALL_SOURCES:
        st = src.get_cached(force=False)
        out.append({
            "key": src.SOURCE_KEY,
            "name": src.SOURCE_NAME,
            "url": getattr(src, "URL_DOC", ""),
            "benchmark": src.BENCHMARK_KEY,
            "ok": st.get("ok", False),
            "error": st.get("error"),
            "fetched_at": st.get("fetched_at", 0),
            "last_ok_at": st.get("last_ok_at", 0),
            "entries": len(st.get("entries", [])),
        })
    return out
