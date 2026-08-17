"""Golden acceptance for the flagship fixture (house_majority).

Goldens are produced by fixtures/make_goldens.py at INTEGRATION (engine
required), reviewed, then committed — never written by hand. Until they
exist on disk, every test here skips with a clear message.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

WHY_ROOT = Path(__file__).resolve().parents[1]
if str(WHY_ROOT) not in sys.path:
    sys.path.insert(0, str(WHY_ROOT))

FIXTURES = WHY_ROOT / "fixtures"
STATE_PATH = FIXTURES / "sources_state.json"
GOLDEN_DIR = WHY_ROOT / "tests" / "golden"
REPORT_GOLDEN = GOLDEN_DIR / "house_majority.report.json"
PROSE_GOLDEN = GOLDEN_DIR / "house_majority.prose.txt"

_SKIP_MSG = (
    "goldens absent — generate them at integration with "
    "`/opt/homebrew/bin/python3.11 fixtures/make_goldens.py` once the engine "
    "modules exist, review, commit. Goldens are never written by hand."
)


def _goldens() -> tuple[dict, str]:
    if not (REPORT_GOLDEN.is_file() and PROSE_GOLDEN.is_file()):
        pytest.skip(_SKIP_MSG)
    report = json.loads(REPORT_GOLDEN.read_text(encoding="utf-8"))
    prose = PROSE_GOLDEN.read_text(encoding="utf-8")
    return report, prose


def test_report_matches_golden_byte_for_byte() -> None:
    _goldens()  # skip gate
    schemas = pytest.importorskip("narve_why.schemas")
    credstate = pytest.importorskip("narve_why.credstate")
    report_mod = pytest.importorskip("narve_why.report")
    raw = json.loads((FIXTURES / "house_majority.json").read_text(encoding="utf-8"))
    req = schemas.parse_request(raw)
    states = credstate.state_for(req, credstate.load_fixture_state(STATE_PATH))
    regenerated = report_mod.to_json(report_mod.explain(req, states))
    assert regenerated.encode("utf-8") == REPORT_GOLDEN.read_bytes(), (
        "report drifted from golden — if intentional, regenerate via "
        "fixtures/make_goldens.py and review the diff"
    )


def test_prose_ten_second_read() -> None:
    """The ten-second assertions: number, market, top driver, dissent,
    sentence budget — everything a reader needs in the first glance."""
    report, prose = _goldens()
    assert report["prose"] in prose  # .prose.txt is the report prose verbatim
    drivers = report["drivers"]
    assert drivers, "flagship report must extract drivers"
    assert drivers[0]["label"] in prose, "top driver label missing from prose"
    assert "62%" in prose, "headline probability missing from prose"
    assert "55" in prose, "market reference (55) missing from prose"
    lowered = prose.lower()
    assert "macro" in lowered or "econom" in lowered, (
        "built-in dissent (macro_model, 0.34 vs 0.62) not mentioned in prose"
    )
    # Negative lookbehind skips abbreviation periods ("U.S. House"): a real
    # sentence here never ends immediately after a capital letter.
    sentences = re.findall(r"(?<![A-Z])[.!?](?:\s|$)", prose.strip())
    assert 3 <= len(sentences) <= 6, (
        f"prose must be 3-6 sentences, counted {len(sentences)}"
    )


def test_market_gap_and_confidence_pins() -> None:
    report, _ = _goldens()
    assert report["probability"] == 0.62
    gap = report["market_gap"]
    assert gap is not None, "kalshi snapshot present — market_gap must not be null"
    assert gap["venue"] == "kalshi", "venue must be deepest liquidity (kalshi 120k)"
    assert gap["gap_pts"] == 7.0  # (0.62 - 0.55) * 100, one decimal
    assert gap["read"] == "market looks cheap"  # gap >= +2.0 pts
    # 4 sources, only 2 above 0.75 credibility, spread 30 pts, 1 resolved:
    # not THIN, not HIGH -> MODERATE per INTERNALS exact rules.
    assert report["confidence_note"].startswith("MODERATE"), report["confidence_note"]
    assert report["question_id"] == "midterm-house-gop-2026"
    assert 2 <= len(report["would_change_it"]) <= 4
