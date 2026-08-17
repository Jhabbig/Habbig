#!/opt/homebrew/bin/python3.11
"""Generate the byte-exact goldens for the flagship fixture.

Run at INTEGRATION, once the engine modules (narve_why.schemas /
credstate / report) are on disk:

    /opt/homebrew/bin/python3.11 fixtures/make_goldens.py

Writes:
    tests/golden/house_majority.report.json   (report.to_json bytes)
    tests/golden/house_majority.prose.txt     (report prose + trailing newline)

Goldens are GENERATED, then reviewed, then committed. Never hand-write or
hand-edit them — regenerate and review the diff instead. Until they exist,
tests/test_prose_golden.py skips with a clear message.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WHY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = WHY_ROOT / "fixtures"
GOLDEN_DIR = WHY_ROOT / "tests" / "golden"


def main() -> int:
    if str(WHY_ROOT) not in sys.path:
        sys.path.insert(0, str(WHY_ROOT))
    try:  # lazy: siblings may not have landed the engine yet
        from narve_why import credstate, report, schemas
    except ImportError as exc:
        sys.stderr.write(
            "make_goldens: narve_why engine modules are not importable yet"
            f" ({exc}).\n"
            "Run this at integration, after schemas.py/credstate.py/report.py"
            " exist. Goldens are generated -> reviewed -> committed; never"
            " written by hand.\n"
        )
        return 1
    raw = json.loads((FIXTURES / "house_majority.json").read_text(encoding="utf-8"))
    req = schemas.parse_request(raw)
    full_state = credstate.load_fixture_state(FIXTURES / "sources_state.json")
    states = credstate.state_for(req, full_state)
    payload = report.to_json(report.explain(req, states))
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    report_path = GOLDEN_DIR / "house_majority.report.json"
    report_path.write_text(payload, encoding="utf-8")
    prose = json.loads(payload)["prose"]
    prose_path = GOLDEN_DIR / "house_majority.prose.txt"
    prose_path.write_text(prose if prose.endswith("\n") else prose + "\n",
                          encoding="utf-8")
    print(f"wrote {report_path}")
    print(f"wrote {prose_path}")
    print("Review the diff before committing — goldens pin bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
