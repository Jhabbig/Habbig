"""Banned-phrase sweep: rendered prose for every fixture + static template scan.

The BANNED list is imported from narve_why.prose (single source of truth) —
never restated here. Standalone-safe: skips cleanly until the engine lands.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

WHY_ROOT = Path(__file__).resolve().parents[1]
if str(WHY_ROOT) not in sys.path:
    sys.path.insert(0, str(WHY_ROOT))

FIXTURES = WHY_ROOT / "fixtures"
STATE_PATH = FIXTURES / "sources_state.json"
PACKAGE_DIR = WHY_ROOT / "narve_why"

REQUEST_FIXTURES = sorted(
    p for p in FIXTURES.glob("*.json") if p.name != "sources_state.json"
)
_IDS = [p.name for p in REQUEST_FIXTURES]


def _banned() -> tuple[str, ...]:
    prose = pytest.importorskip("narve_why.prose")
    banned = tuple(b.lower() for b in prose.BANNED)
    assert banned, "narve_why.prose.BANNED must be non-empty"
    return banned


def _render_prose(path: Path) -> str:
    schemas = pytest.importorskip("narve_why.schemas")
    credstate = pytest.importorskip("narve_why.credstate")
    report_mod = pytest.importorskip("narve_why.report")
    req = schemas.parse_request(json.loads(path.read_text(encoding="utf-8")))
    states = credstate.state_for(req, credstate.load_fixture_state(STATE_PATH))
    report = json.loads(report_mod.to_json(report_mod.explain(req, states)))
    prose = report["prose"]
    assert isinstance(prose, str) and prose.strip(), f"{path.name}: empty prose"
    return prose


@pytest.mark.parametrize("path", REQUEST_FIXTURES, ids=_IDS)
def test_rendered_prose_has_no_banned_phrase(path: Path) -> None:
    banned = _banned()
    lowered = _render_prose(path).lower()
    hits = [b for b in banned if b in lowered]
    assert not hits, f"{path.name}: banned phrase(s) in prose: {hits}"


def _banned_assignment_lines(tree: ast.Module) -> list[tuple[int, int]]:
    """Line ranges of any `BANNED = (...)` assignment — the list itself is
    the one legitimate place these phrases appear in the package."""
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == "BANNED" for t in targets):
            ranges.append((node.lineno, node.end_lineno or node.lineno))
    return ranges


def test_no_banned_phrase_in_package_template_strings() -> None:
    """Static sweep: no banned phrase in ANY string literal in narve_why/*.py
    (f-string parts included), excluding only the BANNED definition itself."""
    banned = _banned()
    assert PACKAGE_DIR.is_dir(), f"package dir missing: {PACKAGE_DIR}"
    offenders: list[str] = []
    for py in sorted(PACKAGE_DIR.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        skip = _banned_assignment_lines(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if any(lo <= node.lineno <= hi for lo, hi in skip):
                continue
            lowered = node.value.lower()
            offenders.extend(
                f"{py.name}:{node.lineno}: {b!r}" for b in banned if b in lowered
            )
    assert not offenders, "banned phrase(s) in package strings:\n" + "\n".join(offenders)
