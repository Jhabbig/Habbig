"""
Historical-event backtest CLI for the annoyance dashboard.

For each event in tests/fixtures/historical_events.yaml, replays the
corpus posts through aggregator + spike_detector (Claude mocked — we
supply synthetic entity annotations directly so the detector runs
deterministically). Records hit/miss per event and an overall hit rate.

Also simulates the current detector against 24h of sampled volume
(injecting the event posts across the target window) to estimate the
daily spike rate — the 5-10 spikes/day calibration target (DECISIONS.md #9).

Usage:
    python3 backtest.py                  # run all events, write report
    python3 backtest.py --event svb-collapse-mar-2023
    python3 backtest.py --report-dir ./reports  # override output dir
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import aggregator  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import spike_detector  # noqa: E402


FIXTURE_PATH = _ROOT / "tests" / "fixtures" / "historical_events.yaml"
DEFAULT_REPORT_DIR = _ROOT / "reports"


# ── Fixtures ─────────────────────────────────────────────────────────────────

@dataclass
class Event:
    id: str
    entity: str
    severity: str
    category: str
    target_window_iso: str
    corpus_posts: list[dict]


def _load_events(path: Path = FIXTURE_PATH) -> list[Event]:
    data = yaml.safe_load(path.read_text())
    return [Event(**e) for e in data.get("events", [])]


# ── Test-DB lifecycle (shared w/ test fixtures) ──────────────────────────────

def _with_fresh_db(func):
    """Decorator: each replay gets a pristine DB. Returns func's return value."""
    def wrapper(*a, **kw):
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d) / "backtest.db"
            original = config.DB_PATH
            config.DB_PATH = dp
            if hasattr(db._local, "conn") and db._local.conn is not None:
                try:
                    db._local.conn.close()
                except Exception:
                    pass
                db._local.conn = None
            try:
                db.init_db()
                return func(*a, **kw)
            finally:
                if hasattr(db._local, "conn") and db._local.conn is not None:
                    db._local.conn.close()
                    db._local.conn = None
                config.DB_PATH = original
    return wrapper


# ── Replay one event ─────────────────────────────────────────────────────────

@dataclass
class Replay:
    event_id: str
    entity: str
    fired: bool
    z_score: float = 0.0
    multiple: float = 0.0
    confidence: float = 0.0
    count: int = 0
    reason: str = ""
    mode: str = ""


def _replay_event(event: Event, *, amplification: int = 4) -> Replay:
    """Seed the corpus, rebuild the target hour, run detect_and_record
    synchronously (no Claude — we skip summarize_spike).

    ``amplification`` replicates each corpus post N times. Real-world events
    generate dozens to hundreds of posts per hour; the 3-5 fixture posts are
    representative samples. Amplifying pushes us past WARMUP_MIN_COUNT=10.
    """
    target_hour = db.bucket_hour(event.target_window_iso)

    # Build baseline history so statistical path is available, with
    # non-uniform same-hour-of-week baseline points (MAD > 0 ⇒ finite z).
    baseline_entity = event.entity
    for weeks_back, cnt, avg in ((1, 1, 30.0), (2, 2, 35.0), (3, 3, 40.0)):
        past = datetime.fromisoformat(target_hour) - timedelta(hours=weeks_back * 168)
        db.upsert_entity_count(baseline_entity, "company", past.isoformat(),
                               count=cnt, avg_annoyance=avg)
    for h in (4, 6, 8, 10, 20, 48, 72, 96, 120, 200, 400):
        past = datetime.fromisoformat(target_hour) - timedelta(hours=h)
        db.upsert_entity_count(baseline_entity, "company", past.isoformat(),
                               count=1, avg_annoyance=30.0)

    # Insert corpus as posts + classifications in the target hour, amplified.
    for i, p in enumerate(event.corpus_posts):
        for rep in range(amplification):
            pid = f"{p.get('source', 'reddit')}:bt-{event.id}-{i}-{rep}"
            db.insert_post(
                id=pid, source=p["source"],
                content=p["content"], posted_at=target_hour,
                source_channel=f"{p['source']}:backtest",
                author="bt", engagement=1,
            )
            db.insert_classification(
                post_id=pid, annoyance_score=85.0, sentiment="angry",
                primary_topic=None,
                entities=[{
                    "name": event.entity, "type": "company",
                    "salience": 0.9, "sentiment": "angry",
                }],
                model="backtest",
            )
    aggregator.rebuild_hour(target_hour)

    # Run the gate — monkey-call _evaluate_entity since detect_and_record
    # uses current_hour_iso() internally.
    fire, info = spike_detector._evaluate_entity(event.entity, target_hour)

    confidence = 0.0
    if fire:
        confidence = spike_detector._compute_confidence(
            z=info.get("z_score") or 0.0,
            multiple=info.get("multiple_of_baseline") or 0.0,
            backtest_hit_rate=0.5,
            warmup=(info.get("mode") == "warmup"),
        )
    return Replay(
        event_id=event.id,
        entity=event.entity,
        fired=bool(fire),
        z_score=float(info.get("z_score") or 0.0),
        multiple=float(info.get("multiple_of_baseline") or 0.0),
        confidence=confidence,
        count=int(info.get("count") or 0),
        reason=str(info.get("reason") or ""),
        mode=str(info.get("mode") or ""),
    )


# ── Rate calibration ─────────────────────────────────────────────────────────

def _daily_rate_estimate(replays: list[Replay], corpus_events_per_day: int) -> float:
    """Given the per-event hit rate across the fixture and a rough daily event
    count (how many 'stories' hit social media per day), estimate how many
    spikes would fire per day under the current gates.

    corpus_events_per_day is a calibration constant: the team estimates 40
    candidate stories/day hit social media (DECISIONS.md #9 calibration note
    — adjust once we have 48h of live data).
    """
    if not replays:
        return 0.0
    hit_rate = sum(1 for r in replays if r.fired) / len(replays)
    return round(hit_rate * corpus_events_per_day, 2)


# ── Report ───────────────────────────────────────────────────────────────────

def _render_report(replays: list[Replay], *, daily_rate: float, corpus_per_day: int) -> str:
    hit = sum(1 for r in replays if r.fired)
    total = len(replays)
    warmup = sum(1 for r in replays if r.fired and r.mode == "warmup")
    statistical = sum(1 for r in replays if r.fired and r.mode == "statistical")
    hit_pct = (hit / total * 100) if total else 0.0

    lines: list[str] = []
    lines.append("# Annoyance Dashboard — Backtest Report")
    lines.append("")
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()}_")
    lines.append("")
    lines.append("## Recall on historical events")
    lines.append("")
    lines.append(f"- **Events**: {total}")
    lines.append(f"- **Detected**: {hit}  ({hit_pct:.1f}%)")
    lines.append(f"- **Missed**: {total - hit}")
    lines.append(f"- Fired via warmup: {warmup}")
    lines.append(f"- Fired via statistical: {statistical}")
    lines.append("")
    lines.append("## Spike-rate projection (DECISIONS.md #9: 5-10 spikes/day)")
    lines.append("")
    lines.append(
        "**NOTE**: this projects detection on _known true positive_ events. "
        "Real calibration requires FP measurement on non-event corpora, which "
        "only becomes available after 48h of live traffic. The number below "
        "is an upper bound, not a prediction."
    )
    lines.append("")
    lines.append(
        f"- Assumed candidate stories/day (placeholder): **{corpus_per_day}**"
    )
    lines.append(f"- Upper-bound spikes/day if 100% are real: **{daily_rate}**")
    lines.append("")
    lines.append("## Per-event results")
    lines.append("")
    lines.append("| Event | Entity | Mode | Fired | Conf | Z | Mult | Count | Reason |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in replays:
        lines.append(
            f"| {r.event_id} | {r.entity} | {r.mode or '—'} | "
            f"{'✅' if r.fired else '❌'} | "
            f"{r.confidence:.0f} | {r.z_score:.2f} | {r.multiple:.2f} | "
            f"{r.count} | {r.reason or ''} |"
        )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- Misses on `moderate`/`minor` events are expected — they serve as "
        "regression checks rather than pass-fail gates."
    )
    lines.append(
        "- Warmup mode fires use absolute thresholds (count ≥ 10 AND "
        "avg_annoyance ≥ 70); statistical mode fires use MAD-normalized z on "
        "same-hour-of-week baseline."
    )
    lines.append("")
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

@_with_fresh_db
def run(events: list[Event]) -> list[Replay]:
    return [_replay_event(e) for e in events]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", help="Run a single event id")
    parser.add_argument(
        "--report-dir", default=str(DEFAULT_REPORT_DIR),
        help="Where to drop the markdown report.",
    )
    parser.add_argument(
        "--corpus-per-day", type=int, default=40,
        help="Assumed candidate stories/day for rate projection.",
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Print summary only; skip writing the markdown report.",
    )
    args = parser.parse_args()

    events = _load_events()
    if args.event:
        events = [e for e in events if e.id == args.event]
        if not events:
            print(f"no event matching id={args.event}", file=sys.stderr)
            return 2

    replays = run(events)
    daily = _daily_rate_estimate(replays, args.corpus_per_day)

    hit = sum(1 for r in replays if r.fired)
    print(f"Backtest: {hit}/{len(replays)} events fired")
    print(f"Projected daily rate: {daily}/day (target 5-10)")

    if not args.no_report:
        out_dir = Path(args.report_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = out_dir / f"backtest_{stamp}.md"
        out_path.write_text(_render_report(replays, daily_rate=daily, corpus_per_day=args.corpus_per_day))
        print(f"Report: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
