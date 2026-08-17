"""narve-why CLI.

    narve-why explain --in payload.json [--db X | --state Y] [--format json|text]

State resolution order: --db (terminal sidecar sqlite), --state (fixture
json), else the bundled fixtures/sources_state.json. ContractError ->
exit 2 with the exact field path on stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from narve_why.credstate import SourceState, load_db_state, load_fixture_state
from narve_why.report import ExplainReport, explain, to_json
from narve_why.schemas import ContractError, parse_request

_DEFAULT_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "sources_state.json"
)

_ARROW = {"up": "↑", "down": "↓"}


def _fmt_pts_as_pct(pts: float) -> str:
    nearest = round(pts)
    if abs(pts - nearest) < 1e-9:
        return f"{int(nearest)}%"
    return f"{pts:.1f}%"


def _pct(value: float) -> str:
    return _fmt_pts_as_pct(value * 100.0)


def render_text(report: ExplainReport, question_text: str) -> str:
    """The ten-second monochrome terminal card (~20 lines, plain text)."""
    lines: list[str] = []
    if report.market_gap is None:
        lines.append(f" {_pct(report.probability)}   no market to compare")
    else:
        gap = report.market_gap
        market_pts = report.probability * 100.0 - gap.gap_pts
        sign = "+" if gap.gap_pts >= 0 else ""
        lines.append(
            f" {_pct(report.probability)}   vs {_fmt_pts_as_pct(market_pts)}"
            f" {gap.venue} ({sign}{gap.gap_pts:.1f} pts — {gap.read})"
        )
    lines.append(f" {report.question_id} — {question_text}")
    lines.append("")
    lines.append(" TOP DRIVERS")
    for driver in report.drivers[:4]:
        arrow = _ARROW.get(driver.direction, "·")
        lines.append(f"   {arrow} {driver.weight:.2f}  {driver.label}")
    lines.append("")
    lines.append(" DISSENT")
    if report.conflicts:
        for conflict in report.conflicts[:2]:
            lines.append(f"   {conflict.note}")
    else:
        lines.append("   none — no credible source dissents")
    lines.append("")
    lines.append(" WATCH")
    for item in report.would_change_it[:3]:
        lines.append(f"   - {item}")
    lines.append("")
    lines.append(" CONFIDENCE")
    lines.append(f"   {report.confidence_note}")
    return "\n".join(lines) + "\n"


def _load_states(args: argparse.Namespace) -> dict[str, SourceState]:
    if args.db:
        return load_db_state(Path(args.db))
    if args.state:
        return load_fixture_state(Path(args.state))
    return load_fixture_state(_DEFAULT_STATE_PATH)


def _cmd_explain(args: argparse.Namespace) -> int:
    in_path = Path(args.in_path)
    try:
        raw_text = in_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"narve-why: cannot read {in_path}: {exc}", file=sys.stderr)
        return 1
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"payload: invalid JSON ({exc})", file=sys.stderr)
        return 2
    if not isinstance(raw, dict):
        print(f"payload: expected a JSON object (got {type(raw).__name__})",
              file=sys.stderr)
        return 2
    try:
        req = parse_request(raw)
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    try:
        states = _load_states(args)
    except OSError as exc:
        print(f"narve-why: cannot load credibility state: {exc}", file=sys.stderr)
        return 1
    report = explain(req, states)
    if args.format == "text":
        sys.stdout.write(render_text(report, req.question_text))
    else:
        sys.stdout.write(to_json(report))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="narve-why",
        description="Explain a computed probability. Never computes it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_explain = sub.add_parser("explain", help="explain one ExplainRequest payload")
    p_explain.add_argument("--in", dest="in_path", required=True,
                           metavar="PAYLOAD", help="ExplainRequest json file")
    source = p_explain.add_mutually_exclusive_group()
    source.add_argument("--db", help="terminal sidecar sqlite db for credibility")
    source.add_argument("--state", help="credibility fixture json")
    p_explain.add_argument("--format", choices=("json", "text"), default="json")
    p_explain.set_defaults(func=_cmd_explain)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
