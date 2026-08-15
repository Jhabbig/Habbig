"""The README quotes measured numbers; they must match the committed manifest.

The effectiveness table drifted from the manifest once already (stale base rates
and recalls survived a regeneration), which is exactly the kind of quiet
dishonesty this toolkit is built to prevent. This parses the README's
effectiveness table and checks every cell against trained/pipeline_manifest.json.

If this fails after you legitimately changed the model, re-run
``python pipeline.py --demo`` and update the README table to the new numbers.
"""

import json
import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_README = os.path.join(_ROOT, "README.md")
_MANIFEST = os.path.join(_ROOT, "trained", "pipeline_manifest.json")

# the table introduced by this header line
_HEADER = "| event | BSS (95% CI) | verdict | MCC | KS | lift@10% | of its max |"


def _manifest():
    with open(_MANIFEST) as f:
        return json.load(f)["events"]


def _readme_rows():
    """Return {event: [cell, ...]} for the effectiveness table."""
    with open(_README) as f:
        lines = f.read().splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == _HEADER)
    except StopIteration:  # pragma: no cover - guarded by its own test below
        return {}
    rows = {}
    for ln in lines[start + 2:]:                 # skip the |---| separator
        if not ln.startswith("|"):
            break
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        rows[cells[0]] = cells[1:]
    return rows


def _num(text):
    """First signed number in a cell, tolerating markdown bold and unicode minus."""
    t = text.replace("**", "").replace("−", "-").replace("×", "")
    m = re.search(r"[-+]?\d*\.?\d+", t)
    return float(m.group()) if m else None


def test_effectiveness_table_exists():
    assert _readme_rows(), f"README effectiveness table not found; expected header:\n{_HEADER}"


def test_readme_covers_exactly_the_manifest_events():
    assert set(_readme_rows()) == set(_manifest())


@pytest.mark.parametrize("event", sorted(_manifest()))
def test_readme_effectiveness_row_matches_manifest(event):
    row = _readme_rows()[event]
    info = _manifest()[event]
    bss_cell, verdict, mcc_cell, ks_cell, lift_cell, ofmax_cell = row[:6]

    assert _num(bss_cell) == pytest.approx(info["brier_skill_score"], abs=0.005)

    lo, hi = info["brier_skill_ci"]
    ci = re.search(r"\[([^\]]+)\]", bss_cell.replace("−", "-"))
    assert ci, f"{event}: no [lo,hi] interval in {bss_cell!r}"
    ci_lo, ci_hi = (float(x) for x in ci.group(1).split(","))
    assert ci_lo == pytest.approx(lo, abs=0.005)
    assert ci_hi == pytest.approx(hi, abs=0.005)

    # the written verdict must agree with the three-state rule the code applies
    if info["brier_skill_score"] <= 0:
        expected = "no skill"
    elif not info["brier_skill_significant"]:
        expected = "within noise"
    else:
        expected = "significant"
    assert expected in verdict.replace("*", "").lower(), (
        f"{event}: README says {verdict!r} but the manifest implies {expected!r}")

    assert _num(mcc_cell) == pytest.approx(info["tuned_mcc"], abs=0.005)
    assert _num(ks_cell) == pytest.approx(info["ks"], abs=0.005)
    assert _num(lift_cell) == pytest.approx(info["lift_at_10pct"], abs=0.05)
    assert _num(ofmax_cell) / 100.0 == pytest.approx(
        info["lift_efficiency_at_10pct"], abs=0.01)
