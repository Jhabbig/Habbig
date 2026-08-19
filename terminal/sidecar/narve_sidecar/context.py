"""Account context — who a source is, and which accounts matter for an event.

Everything here is deterministic and cheap: relevance is plain token overlap
(no embeddings, no LLM), and the DJT rule reports facts about the bias mix of
the sources predicting a question — never colors, just counts.
"""

from __future__ import annotations

import re
import sqlite3

from . import credibility

BIAS_LEVELS = ("left", "lean-left", "center", "lean-right", "right", "unknown")

# A question is DJT-related when its id+title matches any of these.
DJT_RE = re.compile(r"djt|trump|maga|mar-a-lago", re.IGNORECASE)

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def tokens(text: str) -> set[str]:
    """Lowercased tokens split on every non-alphanumeric run."""
    return {t for t in _TOKEN_SPLIT.split(text.lower()) if t}


def context_match(question_text: str, topics: str) -> float:
    """Share of the source's topic tokens that appear in the question's
    id+title tokens. 0.0 when the source has no topics."""
    topic_tokens = tokens(topics)
    if not topic_tokens:
        return 0.0
    return len(topic_tokens & tokens(question_text)) / len(topic_tokens)


def relevant_sources(conn: sqlite3.Connection, question_id: str,
                     question_text: str) -> list[dict]:
    """Who we should be hearing from but aren't: up to 10 sources with topic
    match > 0 and NO prediction on the question, sorted by (match desc,
    credibility desc)."""
    predicting = {r[0] for r in conn.execute(
        "SELECT DISTINCT source_id FROM predictions WHERE question_id = ?",
        (question_id,))}
    out = []
    for s in conn.execute(
            "SELECT id, alpha, beta, bias, region, topics FROM sources"
            " WHERE topics != ''"):
        if s["id"] in predicting:
            continue
        match = context_match(question_text, s["topics"])
        if match <= 0:
            continue
        out.append({
            "source_id": s["id"],
            "credibility": round(credibility.credibility(s["alpha"], s["beta"]), 4),
            "bias": s["bias"], "region": s["region"], "match": round(match, 4),
        })
    out.sort(key=lambda r: (-r["match"], -r["credibility"], r["source_id"]))
    return out[:10]


def source_context(conn: sqlite3.Connection, question_id: str,
                   question_text: str) -> dict:
    """Bias/region mix of the sources PREDICTING the question, plus the DJT
    rule: on DJT-related questions with >=3 predicting sources where one known
    bias bucket holds >60% of the known-bias sources, state the skew as fact."""
    rows = conn.execute(
        "SELECT bias, region FROM sources WHERE id IN (SELECT DISTINCT"
        " source_id FROM predictions WHERE question_id = ?)",
        (question_id,)).fetchall()
    bias_spread = {b: 0 for b in BIAS_LEVELS}
    regions: dict[str, int] = {}
    for r in rows:
        bias = r["bias"] if r["bias"] in BIAS_LEVELS else "unknown"
        bias_spread[bias] += 1
        region = r["region"] or "unknown"
        regions[region] = regions.get(region, 0) + 1
    region_spread = dict(
        sorted(regions.items(), key=lambda kv: (-kv[1], kv[0]))[:5])
    djt_related = bool(DJT_RE.search(question_text))
    skew_note = None
    if djt_related and len(rows) >= 3:
        known = {b: n for b, n in bias_spread.items()
                 if b != "unknown" and n > 0}
        total_known = sum(known.values())
        if total_known:
            bucket, top = max(known.items(), key=lambda kv: kv[1])
            if top > 0.6 * total_known:
                skew_note = (f"{top} of {total_known} known-bias sources lean"
                             f" {bucket} — weigh accordingly")
    return {"bias_spread": bias_spread, "region_spread": region_spread,
            "djt_related": djt_related, "skew_note": skew_note}
