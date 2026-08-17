"""Template prose for narve_why reports.

Every sentence is derived from a structured report field — no free
generation. `render_prose` refuses (AssertionError) to emit any BANNED
filler phrase. `llm_polish` is the documented seam for an optional LLM
rewrite; v0 returns its input unchanged.
"""
from __future__ import annotations

import os
from typing import Any

BANNED = (
    "it's complicated",
    "it is complicated",
    "many factors",
    "time will tell",
    "hard to say",
    "uncertain times",
    "only time",
    "remains to be seen",
)

_DIRECTION_PHRASE = {"up": "pushing it up", "down": "dragging it down"}


def _pct(value: float) -> str:
    """Format a [0,1] fraction: 0.62 -> '62%', 0.615 -> '61.5%'."""
    pts = value * 100.0
    nearest = round(pts)
    if abs(pts - nearest) < 1e-9:
        return f"{int(nearest)}%"
    return f"{pts:.1f}%"


def _sentence_number(parts: dict[str, Any]) -> str:
    prob = _pct(float(parts["probability"]))
    market = parts.get("market")
    if market is None:
        return f"{prob} with no market to compare."
    return f"{prob} vs a {_pct(float(market['yes_price']))} market."


def _sentence_drivers(parts: dict[str, Any]) -> str | None:
    drivers = list(parts.get("drivers") or [])
    if not drivers:
        return None
    # Explain the number the way a reader parses it: lead with the strongest
    # driver pointing the way the number leans vs the market (or just the
    # heaviest driver when there is no market), then name the counterweight.
    gap = parts.get("market_gap")
    lead_dir = "up" if gap is None or float(gap["gap_pts"]) >= 0 else "down"
    lead = next((d for d in drivers if d["direction"] == lead_dir), drivers[0])
    counter = next((d for d in drivers if d["direction"] != lead["direction"]), None)
    lead_clause = f"{lead['label']}, {_DIRECTION_PHRASE[str(lead['direction'])]}"
    if counter is None:
        second = next((d for d in drivers if d is not lead), None)
        if second is None:
            return f"Driving it: {lead_clause}."
        return (
            f"Driving it: {lead_clause};"
            f" {second['label']}, {_DIRECTION_PHRASE[str(second['direction'])]}."
        )
    return (
        f"Driving it: {lead_clause}; the counterweight is"
        f" {counter['label']}, {_DIRECTION_PHRASE[str(counter['direction'])]}."
    )


def _sentence_dissent(parts: dict[str, Any]) -> str:
    conflicts = list(parts.get("conflicts") or [])
    if not conflicts:
        return "No credible source dissents."
    return f"Dissent: {conflicts[0]['note']}."


def _sentence_watch(parts: dict[str, Any]) -> str | None:
    watch = [str(item) for item in list(parts.get("would_change_it") or [])[:2]]
    if not watch:
        return None
    return f"Watch: {'; '.join(watch)}."


def render_prose(report_parts: dict[str, Any]) -> str:
    """3-6 sentences: number vs market, top drivers, dissent, watch, confidence."""
    sentences = [_sentence_number(report_parts)]
    drivers_sentence = _sentence_drivers(report_parts)
    if drivers_sentence is not None:
        sentences.append(drivers_sentence)
    sentences.append(_sentence_dissent(report_parts))
    watch_sentence = _sentence_watch(report_parts)
    if watch_sentence is not None:
        sentences.append(watch_sentence)
    sentences.append(f"Confidence: {report_parts['confidence_note']}.")
    prose = " ".join(sentences)
    lowered = prose.lower()
    for phrase in BANNED:
        assert phrase not in lowered, f"banned phrase in rendered prose: {phrase!r}"
    return prose


def llm_polish(prose: str, structured: dict[str, Any]) -> str:
    """LLM polish seam — v0 is the identity function.

    When NARVE_WHY_LLM_KEY is set, a future version may reword `prose`
    (grounded strictly in `structured`; reword only, never add claims).
    v0 deliberately returns the input unchanged so structured output and
    prose stay byte-deterministic.
    """
    _ = os.environ.get("NARVE_WHY_LLM_KEY")  # reserved for the future polish path
    _ = structured
    return prose
