"""Claude Sonnet plain-English source summary.

Generated on-demand when a source detail page is viewed, cached 30 days
against the ``source_summaries`` table (migration 052 — backward-compat
with 029).

The summariser reads from whatever source/prediction tables the
credibility pipeline has populated and hands Claude a compact stat blob.
It does NOT import db.py — every DB touch is via a local sqlite3
connection, so the module stays independently testable.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from ai import client


log = logging.getLogger("ai.source_summariser")


SUMMARY_TTL_SECONDS = 30 * 86400
MIN_PREDICTIONS = 5
SUMMARY_MAX_TOKENS = 600


SUMMARY_SYSTEM_PROMPT = """\
You write short factual profile summaries for a prediction-market analytics
site. Your audience is a trader deciding whether to trust a specific source.

Given structured stats, write 3–5 sentences (≈60–90 words) in plain prose:
  - topics they predict on most
  - their accuracy on tracked predictions
  - any noteworthy strength or weakness visible in the numbers

Rules:
  - Start with "@<handle>" — not "This source"
  - No markdown, no bullets
  - Don't invent facts not in the stats
  - Don't close with "narve.ai" advertising

Output ONLY the prose.
"""


# ── DB path (same resolver as ai/cache.py) ──────────────────────────────────


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ── Read path — tolerant of several schema dialects ─────────────────────────
#
# Different branches of the credibility pipeline have named things slightly
# differently (sources vs source_credibility, etc.). Use PRAGMA to probe.


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _load_source(conn: sqlite3.Connection, handle: str) -> Optional[dict]:
    if _table_exists(conn, "source_credibility"):
        row = conn.execute(
            "SELECT * FROM source_credibility WHERE source_handle = ?",
            (handle,),
        ).fetchone()
        if row:
            return dict(row)
    if _table_exists(conn, "sources"):
        row = conn.execute(
            "SELECT * FROM sources WHERE handle = ? OR source_handle = ?",
            (handle, handle),
        ).fetchone()
        if row:
            return dict(row)
    return None


def _load_category_breakdown(conn: sqlite3.Connection, handle: str) -> list[dict]:
    if not _table_exists(conn, "source_category_credibility"):
        return []
    rows = conn.execute(
        "SELECT category, category_credibility, prediction_count, correct_count "
        "FROM source_category_credibility "
        "WHERE source_handle = ? "
        "ORDER BY prediction_count DESC LIMIT 10",
        (handle,),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_recent_predictions(conn: sqlite3.Connection, handle: str, limit: int = 20) -> list[dict]:
    if not _table_exists(conn, "predictions"):
        return []
    rows = conn.execute(
        "SELECT content, category, direction, resolved, resolved_correct "
        "FROM predictions WHERE source_handle = ? "
        "ORDER BY extracted_at DESC LIMIT ?",
        (handle, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Public entrypoint ────────────────────────────────────────────────────────


def _read_cached(conn: sqlite3.Connection, handle: str, now: int) -> Optional[dict]:
    if not _table_exists(conn, "source_summaries"):
        return None
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(source_summaries)")}
    pk_col = "handle" if "handle" in cols else "source_handle"
    cvu_col = "cache_valid_until" if "cache_valid_until" in cols else None
    row = conn.execute(
        f"SELECT * FROM source_summaries WHERE {pk_col} = ?",
        (handle,),
    ).fetchone()
    if row is None:
        return None
    row = dict(row)
    if cvu_col and row.get(cvu_col) and row[cvu_col] <= now:
        return None
    return row


def _write_cached(
    conn: sqlite3.Connection,
    handle: str,
    summary: str,
    model: str,
    now: int,
    predictions_considered: int,
) -> None:
    if not _table_exists(conn, "source_summaries"):
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(source_summaries)")}
    pk_col = "handle" if "handle" in cols else "source_handle"
    has_model = "model" in cols
    has_cvu = "cache_valid_until" in cols
    has_pc = "predictions_considered" in cols

    has_generated_by = "generated_by" in cols
    conn.execute(f"DELETE FROM source_summaries WHERE {pk_col} = ?", (handle,))
    fields = [pk_col, "summary", "generated_at"]
    vals: list[Any] = [handle, summary, now]
    if has_model:
        fields.append("model"); vals.append(model)
    if has_generated_by:
        # Older migration shipped generated_by NOT NULL; satisfy it by
        # mirroring the model string. Post-052 branches can rely on model.
        fields.append("generated_by"); vals.append(model)
    if has_cvu:
        fields.append("cache_valid_until"); vals.append(now + SUMMARY_TTL_SECONDS)
    if has_pc:
        fields.append("predictions_considered"); vals.append(predictions_considered)
    placeholders = ",".join("?" * len(fields))
    conn.execute(
        f"INSERT INTO source_summaries ({','.join(fields)}) VALUES ({placeholders})",
        tuple(vals),
    )
    conn.commit()


def _build_user_message(
    handle: str,
    src: dict,
    categories: list[dict],
    predictions: list[dict],
) -> str:
    total = int(src.get("total_predictions") or 0)
    correct = int(src.get("correct_predictions") or 0)
    accuracy = round(100 * correct / total, 1) if total else 0.0
    global_cred = round(float(src.get("global_credibility") or 0.5), 3)

    lines: list[str] = [
        f"Source handle: @{handle}",
        f"Global credibility: {global_cred}",
        f"Accuracy: {correct}/{total} = {accuracy}%",
        f"Categories active: {int(src.get('categories_active') or 0)}",
    ]
    if categories:
        lines += ["", "Category breakdown:"]
        for c in categories:
            ct = int(c.get("prediction_count") or 0)
            cc = int(c.get("correct_count") or 0)
            cc_pct = round(100 * cc / ct, 1) if ct else 0.0
            cred = round(float(c.get("category_credibility") or 0.5), 3)
            lines.append(
                f"  - {c.get('category','?')}: credibility={cred}, "
                f"{cc}/{ct} correct ({cc_pct}%)"
            )
    if predictions:
        lines += ["", "Recent predictions:"]
        for p in predictions[:15]:
            content = (p.get("content") or "")[:120]
            cat = p.get("category") or "other"
            status = "resolved" if p.get("resolved") else "open"
            if p.get("resolved"):
                status += " (correct)" if p.get("resolved_correct") else " (wrong)"
            lines.append(f"  - [{cat}] {status}: {content}")
    return "\n".join(lines)


def _fallback_summary(handle: str, total: int) -> str:
    if total == 0:
        return (
            f"@{handle} has not yet made enough tracked predictions for a "
            f"narrative summary. As their prediction history grows, this "
            f"section will describe their areas of focus and relative strengths."
        )
    return (
        f"@{handle} has {total} tracked prediction{'s' if total != 1 else ''} "
        f"on file. A full narrative summary will generate once their history "
        f"passes the threshold for a meaningful category-level breakdown."
    )


async def _call_claude(user_message: str) -> tuple[Optional[str], Any]:
    text = await client.call_claude(
        feature="summarisation",
        system=SUMMARY_SYSTEM_PROMPT,
        user=user_message,
        model=client.ANTHROPIC_MODELS["summarisation"],
        max_tokens=SUMMARY_MAX_TOKENS,
    )
    return text, (True if text is not None else None)


async def generate_source_summary(
    source_handle: str,
    *,
    force: bool = False,
) -> dict:
    """Return (and cache) a summary dict for *source_handle*.

    Shape: ``{handle, summary, model, generated_at, predictions_considered}``.
    Never raises — callers can render blindly.
    """
    handle = (source_handle or "").strip().lstrip("@")
    now = int(time.time())
    if not handle:
        return {
            "handle": "", "summary": "", "model": None,
            "generated_at": now, "predictions_considered": 0,
        }

    conn = _connect()
    try:
        if not force:
            cached = _read_cached(conn, handle, now)
            if cached is not None:
                client.log_claude_usage_row(
                    feature="summarisation",
                    model=client.ANTHROPIC_MODELS["summarisation"],
                    cached_hit=True,
                )
                return {
                    "handle": handle,
                    "summary": cached.get("summary") or "",
                    "model": cached.get("model"),
                    "generated_at": cached.get("generated_at") or now,
                    "predictions_considered": cached.get("predictions_considered") or 0,
                }

        src = _load_source(conn, handle)
        if not src:
            summary = _fallback_summary(handle, 0)
            _write_cached(conn, handle, summary,
                          "fallback_no_data", now, 0)
            return {
                "handle": handle, "summary": summary,
                "model": "fallback_no_data",
                "generated_at": now, "predictions_considered": 0,
            }

        total = int(src.get("total_predictions") or 0)
        categories = _load_category_breakdown(conn, handle)
        predictions = _load_recent_predictions(conn, handle, limit=20)

        if total < MIN_PREDICTIONS:
            summary = _fallback_summary(handle, total)
            _write_cached(conn, handle, summary,
                          "fallback_low_data", now, total)
            return {
                "handle": handle, "summary": summary,
                "model": "fallback_low_data",
                "generated_at": now, "predictions_considered": total,
            }

        user_msg = _build_user_message(handle, src, categories, predictions)
        raw, _resp = await _call_claude(user_msg)
        # call_claude already logged success, failure, or kill-switch.

        if raw is not None:
            model_used = client.ANTHROPIC_MODELS["summarisation"]
            summary_text = raw.strip()[:1200]
            if not summary_text:
                summary_text = _fallback_summary(handle, total)
                model_used = "fallback_empty"
        else:
            summary_text = _fallback_summary(handle, total)
            model_used = "fallback_unavailable"

        _write_cached(conn, handle, summary_text, model_used, now, len(predictions))
        return {
            "handle": handle, "summary": summary_text,
            "model": model_used, "generated_at": now,
            "predictions_considered": len(predictions),
        }
    finally:
        conn.close()
