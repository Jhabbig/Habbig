#!/usr/bin/env python3
"""narve end-to-end pipeline runner — the sealed info -> % -> calibration loop.

This orchestrates the four narve modules that already exist into one runnable
backtest over REAL data:

    1. intelligence.narve_calibration.fetch_resolved_markets()
         -> pull decisively-resolved Polymarket markets (the ANSWER KEY).
    2. intelligence.info_ingest.gather(query)
         -> for each event, pull REAL info from Reddit / HN / NPR / The Hill.
    3. intelligence.narve_llm.forecast_event(question, info_items)
         -> narve reasons over the info and produces its OWN probability
            (+ digest, source_spread, political_lean). NEVER blends the price.
    4. intelligence.narve_calibration.grade(forecasts)
         -> score narve's probabilities against the real outcomes
            (directional accuracy, Brier, vs coin-flip).

PRODUCT MODEL (do not deviate): narve ingests INFO, generates its own number,
and is graded on calibration versus what actually happened. The market price is
the answer key and an edge benchmark — it is NEVER an input to the forecast.

REALITY OF THE DATA:
  * The resolved markets (outcomes) are REAL (live Polymarket gamma).
  * The info items (Reddit/HN/NPR/Hill) are REAL (live feeds).
  * The PROBABILITY is the real model's only when run live
    (NARVE_OFFLINE unset + ANTHROPIC_API_KEY set). With --offline the number
    comes from the clearly-labelled keyword heuristic in narve_llm — that run
    proves the PIPELINE works end-to-end on real info + real outcomes, but the
    probability QUALITY is heuristic, not the model's. This is stated in the
    output file and the printed summary so it can never be mistaken.

A NOTE ON LOOKAHEAD: this is a present-time plumbing/eval harness, not a
point-in-time historical backtest. The info feeds return CURRENT items, while
the outcomes are already resolved, so for old markets the info post-dates
resolution. That is an honest limitation of free real-time feeds (they have no
deep historical search reachable from this host) and is recorded in the output
under `caveats`. The loop's mechanics — ingest -> forecast -> grade — are
exactly what runs on live, not-yet-resolved markets.

USAGE:
    python3 scripts/run_narve_pipeline.py --offline --max-events 12
    NARVE_FORECAST_MODEL=... ANTHROPIC_API_KEY=... \
        python3 scripts/run_narve_pipeline.py --max-events 12   # live model
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make the repo root importable when run as `python3 scripts/run_narve_pipeline.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from intelligence import info_ingest, narve_calibration, narve_llm  # noqa: E402

_OUT_PATH = _REPO_ROOT / "data" / "backtest" / "narve_pipeline_run.json"

# Outcome labels that are NOT clean YES/NO binaries we can search news/social on.
# fetch_resolved_markets already enforces decisive 2-way resolution, but some of
# those 2-way markets are price/score scalars dressed as Long/Short or numeric
# brackets — not a yes/no event with a searchable narrative. We prefer to skip
# those when we have enough true yes/no events.
_NON_YESNO_LABELS = {"long", "short", "over", "under", "up", "down"}

# Question phrasings that are pure numeric-outcome / exact-value lines with no
# narrative to forecast from free text feeds (crypto/price/score brackets).
_NOVELTY_QUESTION_RE = re.compile(
    r"\b(what will the (price|value)|usd price|exact (score|number)|how many points)\b",
    re.IGNORECASE,
)


def _log(msg: str) -> None:
    print(f"[pipeline] {msg}", file=sys.stderr)


def _is_searchable(market: dict) -> bool:
    """True if the question has enough readable NON-novelty yes/no narrative to
    search news/social on. We skip pure price/score scalar lines when possible."""
    q = (market.get("question") or "").strip()
    if len(q) < 15:
        return False  # too thin to derive a query from
    outcomes = [str(o).strip().lower() for o in (market.get("outcomes") or [])]
    # A clean binary is Yes/No. Long/Short, numeric brackets etc. are scalar.
    if any(o in _NON_YESNO_LABELS for o in outcomes):
        return False
    if _NOVELTY_QUESTION_RE.search(q):
        return False
    return True


# Stopwords stripped when turning a question into a search query.
_QUERY_STOP = {
    "will", "the", "a", "an", "be", "to", "of", "in", "on", "for", "by",
    "before", "after", "than", "and", "or", "is", "are", "was", "were",
    "at", "with", "as", "this", "that", "their", "his", "her", "its", "it",
    "get", "win", "wins", "which", "what", "who", "whom", "whose", "how",
}


def derive_query(question: str) -> str:
    """Turn a market question into a compact search query: keep the salient
    content words (proper nouns / specifics), drop boilerplate, cap length.

    e.g. "Will Trump win the 2020 U.S. presidential election?" ->
         "Trump 2020 U.S. presidential election"
    """
    q = question.strip().rstrip("?")
    # Tokenise on whitespace but keep tokens with internal dots (U.S.) intact.
    tokens = re.findall(r"[A-Za-z0-9.$%&'-]+", q)
    kept: list[str] = []
    for tok in tokens:
        low = tok.lower().strip(".")
        if low in _QUERY_STOP:
            continue
        kept.append(tok)
    # Cap to the first ~8 salient tokens so the query stays focused.
    query = " ".join(kept[:8]).strip()
    return query or question.strip()


def run(max_events: int, *, limit_per: int = 12) -> dict:
    """Run the full pipeline and return the assembled report dict."""
    offline = narve_llm._offline_enabled()
    mode = "offline-heuristic" if offline else "live-model"
    _log(f"mode = {mode} (NARVE_OFFLINE={'1' if offline else 'unset'})")
    if not offline:
        _log(f"forecast model = {narve_llm._model()}")

    # 1. ANSWER KEY: pull resolved markets. Grab a generous pool so we can skip
    #    scalar/novelty lines and still reach max_events searchable ones.
    pool_size = max(max_events * 6, 60)
    _log(f"fetching up to {pool_size} resolved markets (answer key)...")
    markets = narve_calibration.fetch_resolved_markets(max_markets=pool_size)
    _log(f"got {len(markets)} decisively-resolved markets")
    if not markets:
        raise SystemExit(
            "No resolved markets returned (network down or feed empty). "
            "Cannot run pipeline on fabricated data — aborting honestly."
        )

    # Prefer searchable yes/no narrative markets; fall back to whatever exists.
    searchable = [m for m in markets if _is_searchable(m)]
    used_fallback = False
    if len(searchable) < max_events:
        used_fallback = True
        _log(
            f"only {len(searchable)} clean yes/no markets; topping up from the "
            f"remaining pool to reach {max_events}"
        )
        extras = [m for m in markets if m not in searchable]
        searchable = searchable + extras
    chosen = searchable[:max_events]
    _log(f"selected {len(chosen)} events to forecast")

    events: list[dict] = []
    forecasts_for_grade: list[dict] = []
    skipped: list[dict] = []

    for i, market in enumerate(chosen, 1):
        question = market["question"]
        query = derive_query(question)
        _log(f"[{i}/{len(chosen)}] '{question[:70]}' -> query={query!r}")

        # 2. REAL INFO for this event.
        try:
            info_items = info_ingest.gather(query, limit_per=limit_per)
        except Exception as exc:  # one bad gather must not kill the run
            _log(f"  gather failed: {exc!r} — skipping event")
            skipped.append({"question": question, "reason": f"gather error: {exc!r}"})
            continue

        if not info_items:
            _log("  no info items returned (feeds empty/flaky) — skipping event")
            skipped.append({"question": question, "reason": "no info items"})
            continue

        # 3. narve's OWN probability from the info. Never blends the price.
        try:
            fc = narve_llm.forecast_event(question, info_items)
        except Exception as exc:
            _log(f"  forecast failed: {exc!r} — skipping event")
            skipped.append({"question": question, "reason": f"forecast error: {exc!r}"})
            continue

        narve_p = fc["probability_yes"]
        resolved = market["resolved_outcome"]
        narve_says_yes = narve_p >= 0.5
        correct = (narve_says_yes) == (resolved == 1)

        events.append({
            "market_id": market["market_id"],
            "question": question,
            "search_query": query,
            "n_info_items": fc["n_items"],
            "narve_probability_yes": narve_p,
            "narve_confidence": fc["confidence"],
            "digest": fc["digest"],
            "drivers": fc["drivers"],
            "source_spread": fc["source_spread"],
            "political_lean": fc["political_lean"],
            "resolved_outcome": resolved,            # 1 = YES won, 0 = NO won
            "resolved_label": (market.get("outcomes") or ["Yes", "No"])[0 if resolved == 1 else 1]
                              if market.get("outcomes") else ("Yes" if resolved == 1 else "No"),
            "resolved_at": market.get("resolved_at"),
            "narve_correct": bool(correct),
        })
        forecasts_for_grade.append({
            "market_id": market["market_id"],
            "narve_probability": narve_p,
            "resolved_outcome": resolved,
        })

    # 4. GRADE narve against the real outcomes.
    grade = narve_calibration.grade(forecasts_for_grade)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "forecast_model": (None if offline else narve_llm._model()),
        "data_provenance": {
            "outcomes": "REAL — live Polymarket gamma resolved markets (answer key)",
            "info_items": "REAL — live Reddit/HN/NPR/The Hill feeds",
            "probability": (
                "OFFLINE HEURISTIC (labelled keyword scan) — NOT the model; "
                "proves the pipeline runs on real info + real outcomes"
                if offline else
                "REAL — narve forecast model over the real info items"
            ),
        },
        "caveats": [
            "Market price is the answer key + edge benchmark only; it is NEVER "
            "blended into narve's probability.",
            "Free real-time feeds have no deep historical search reachable here, "
            "so for already-resolved markets the info is current and post-dates "
            "resolution. This is a present-time plumbing/eval harness; the same "
            "ingest->forecast->grade loop runs unchanged on live, unresolved "
            "markets at point-in-time.",
        ]
        + (
            ["Probability is the OFFLINE keyword heuristic, not the model — "
             "directional accuracy/Brier below reflect the pipeline + heuristic, "
             "not narve's true forecasting skill. Re-run live for that."]
            if offline else []
        )
        + (
            ["Fewer clean yes/no markets than requested were available; the pool "
             "was topped up with remaining resolved markets (some may be scalar)."]
            if used_fallback else []
        ),
        "n_events": len(events),
        "n_skipped": len(skipped),
        "skipped": skipped,
        "grade": grade,
        "events": events,
    }
    return report


def _print_summary(report: dict) -> None:
    g = report["grade"]
    n = report["n_events"]
    acc = g.get("accuracy")
    brier = g.get("brier")
    vs = g.get("vs_coinflip")

    def _pct(x):
        return "n/a" if x is None else f"{x * 100:.1f}%"

    def _num(x):
        return "n/a" if x is None else f"{x:.4f}"

    print("=" * 64)
    print("narve pipeline run — SUMMARY")
    print("=" * 64)
    print(f"mode                 : {report['mode']}")
    if report.get("forecast_model"):
        print(f"forecast model       : {report['forecast_model']}")
    print(f"events processed     : {n}")
    print(f"events skipped       : {report['n_skipped']}")
    print(f"directional accuracy : {_pct(acc)}  ({g.get('n')} graded)")
    print(f"Brier score          : {_num(brier)}  (lower is better; None if n<10)")
    print(f"vs coin-flip (0.25)  : {_num(vs)}  (>0 means narve beats 50/50)")
    print("-" * 64)
    print("data provenance:")
    for k, v in report["data_provenance"].items():
        print(f"  {k:12s}: {v}")
    print("-" * 64)
    print("caveats:")
    for c in report["caveats"]:
        print(f"  - {c}")
    print("=" * 64)


def main() -> None:
    ap = argparse.ArgumentParser(description="narve end-to-end pipeline runner.")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="Set NARVE_OFFLINE=1 so it runs without an API key (labelled heuristic).",
    )
    ap.add_argument(
        "--max-events", type=int, default=12, help="Number of events to forecast."
    )
    ap.add_argument(
        "--limit-per", type=int, default=12, help="Info items to pull per source."
    )
    args = ap.parse_args()

    if args.offline:
        os.environ["NARVE_OFFLINE"] = "1"

    t0 = time.time()
    report = run(args.max_events, limit_per=args.limit_per)
    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _log(f"wrote {_OUT_PATH} ({_OUT_PATH.stat().st_size} bytes) in {time.time()-t0:.0f}s")
    _print_summary(report)


if __name__ == "__main__":
    main()
