"""AI-generated 'what changed and why' narrative.

Calls the Claude API (Haiku — cheap and fast) on the current data snapshot
and returns a 3-sentence read of where the mood index is, what moved it,
and what to watch. Caches the result against a signature of the input
data so we only spend tokens when the underlying numbers actually move.

Graceful degradation: if ANTHROPIC_API_KEY is unset, or the SDK isn't
installed, or the API call fails, the rest of the dashboard renders fine
and the narrative slot shows a friendly fallback.
"""

from __future__ import annotations

import logging
import os
import time
from threading import Lock

log = logging.getLogger(__name__)

try:
    from anthropic import Anthropic
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

MODEL = os.environ.get("VOTER_PULSE_NARRATIVE_MODEL", "claude-haiku-4-5-20251001")

SYSTEM_PROMPT = """You write the daily summary at the top of "Voter Pulse", a non-partisan US dashboard tracking how voters feel and how their lives are going. Your job is one block of plain prose, no headings, no bullets.

Hard rules:
- Exactly three sentences. Max 70 words total.
- Sentence 1: the current mood index value, its verbal label, and the headline level (e.g. "near the historical lows", "the highest since 2019" — only claim this if the data supports it; otherwise just state the level).
- Sentence 2: name the single biggest driver (the sub-score or indicator that explains the move) with the specific number.
- Sentence 3: one concrete thing to watch — a specific upcoming release or indicator, not generic advice.

Tone: calm, factual, non-partisan. No flourishes ("notably", "interestingly", "amid", "as we see"). No political commentary. No metaphors. Plain numbers only. If a number is missing or stale, say "data unavailable" — never speculate."""


# ── Cache ────────────────────────────────────────────────────────────────────
_CACHE: dict = {"narrative": None, "signature": None, "generated_at": 0.0, "model": MODEL}
_lock = Lock()


def _signature(mood: dict, life: dict, polls: dict, backtest: dict) -> tuple:
    """Stable hash of inputs that should regenerate the narrative.

    We key off the FRED fetch timestamp (so a new monthly release triggers
    a regen) plus the key headline numbers (so manual ?force=true on the
    mood endpoint also invalidates)."""
    return (
        round(mood.get("overall") or 0, 1),
        round(mood.get("misery_index") or 0, 1),
        round(mood.get("expectations_gap") or 0, 1),
        life.get("fetched_at"),
        round((polls.get("latest_approval") or {}).get("approve") or 0, 1),
        ((backtest.get("headline") or {}).get("accuracy_pct") or 0),
    )


def _build_data_block(mood: dict, life: dict, polls: dict, backtest: dict) -> str:
    lines: list[str] = []
    if mood.get("overall") is not None:
        lines.append(f"National mood index: {mood['overall']:.0f} ({mood.get('label', '—')})")
    for name, sub in (mood.get("subscores") or {}).items():
        if sub.get("score") is not None:
            lines.append(f"  {name} sub-score: {sub['score']:.0f}")
    if mood.get("misery_index") is not None:
        lines.append(f"Misery index (UNRATE + CPI YoY): {mood['misery_index']:.1f}")
    if mood.get("expectations_gap") is not None:
        lines.append(f"Inflation-expectations gap (MICH minus realised): {mood['expectations_gap']:+.1f} pp")
    key_ids = {"CPIAUCSL", "UNRATE", "MORTGAGE30US", "GASREGW", "UMCSENT", "MICH"}
    for s in (life.get("series") or []):
        if s.get("series_id") in key_ids and s.get("latest"):
            yoy_str = f", YoY {s['yoy_pct']:+.1f}%" if s.get("yoy_pct") is not None else ""
            fy_str  = f", 4y {s['four_year_pct']:+.1f}%" if s.get("four_year_pct") is not None else ""
            lines.append(f"{s.get('label')}: {s['latest']['value']:.2f} {s.get('units')}{yoy_str}{fy_str}")
    la = polls.get("latest_approval") or {}
    if la.get("approve") is not None:
        lines.append(f"Approval (latest pollster mean, {la.get('politician') or 'current'}): "
                     f"{la['approve']:.1f}% approve / {la['disapprove']:.1f}% disapprove (n={la.get('n_polls')})")
    lg = polls.get("latest_generic_ballot") or {}
    if lg.get("margin_d_minus_r") is not None:
        lines.append(f"Generic ballot (D minus R): {lg['margin_d_minus_r']:+.1f} pp")
    if (backtest.get("headline") or {}).get("accuracy_pct") is not None:
        h = backtest["headline"]
        lines.append(f"Election backtest: at the {h['horizon_months']}-month horizon the mood "
                     f"index has called {h['correct']} of {h['n']} elections ({h['accuracy_pct']:.0f}%).")
    return "\n".join(lines) if lines else "(no data available)"


def generate(mood: dict, life: dict, polls: dict, backtest: dict, force: bool = False) -> dict:
    sig = _signature(mood, life, polls, backtest)
    now = time.time()
    with _lock:
        if not force and _CACHE["narrative"] and _CACHE["signature"] == sig:
            return {
                "narrative": _CACHE["narrative"],
                "generated_at": _CACHE["generated_at"],
                "cached": True,
                "model": _CACHE["model"],
            }

    if not _SDK_AVAILABLE:
        return {"narrative": None, "generated_at": 0, "cached": False,
                "error": "anthropic SDK not installed"}
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"narrative": None, "generated_at": 0, "cached": False,
                "error": "ANTHROPIC_API_KEY not set"}

    data_block = _build_data_block(mood, life, polls, backtest)
    client = Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=220,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    "Today's snapshot for Voter Pulse:\n\n"
                    f"{data_block}\n\n"
                    "Write the 3-sentence summary now."
                ),
            }],
        )
        text = "".join(block.text for block in resp.content if getattr(block, "type", None) == "text").strip()
    except Exception as exc:
        log.warning("narrative generation failed: %s", exc)
        return {"narrative": None, "generated_at": 0, "cached": False, "error": str(exc)}

    with _lock:
        _CACHE["narrative"] = text
        _CACHE["signature"] = sig
        _CACHE["generated_at"] = now
    return {
        "narrative": text,
        "generated_at": now,
        "cached": False,
        "model": MODEL,
    }
