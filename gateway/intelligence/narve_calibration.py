"""Grade narve's forecasts against real, resolved market outcomes.

PRODUCT MODEL (do not deviate): narve ingests INFO, generates its OWN
probability, and is judged purely on CALIBRATION versus what actually
happened. The market price is the answer key — never an input to the number.
This module closes that loop: it pulls decisively-resolved Polymarket markets
(the ground truth) and scores a batch of narve probabilities against them.

Two halves, cleanly separated so the scorer is testable without network:

  fetch_resolved_markets()  -- the ONLY networked function. Paginates the
      Polymarket gamma "closed markets" endpoint via curl (urllib is 403 from
      this host; the IP is flaky so we retry-on-empty) and keeps ONLY markets
      that resolved decisively. Decisiveness + YES/NO direction reuse the
      battle-tested `integrations.polymarket_ingest.decisive_outcome` so the
      {0,1}/abs>0.9 rule lives in exactly one place.

  grade(forecasts)          -- pure. Given narve probabilities paired with the
      real outcomes, computes directional accuracy, the Brier score (via
      `credibility.calibration.compute_brier_score`, reusing the project's
      one Brier implementation), and the ten-bin reliability diagram. Also
      reports `vs_coinflip`: how much better narve's Brier is than the 0.25 a
      blind 50/50 guesser scores — positive means narve beats the coin.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any, Optional

from integrations.polymarket_ingest import decisive_outcome
from credibility.calibration import (
    compute_brier_score,
    reliability_diagram_data,
)

# --------------------------------------------------------------------------- #
# Network plumbing (curl; urllib is 403-blocked from this host)
# --------------------------------------------------------------------------- #
_UA = "Mozilla/5.0 (narve)"
_GAMMA = "https://gamma-api.polymarket.com/markets"
_PAGE = 100  # gamma page size we paginate with


def _log(msg: str) -> None:
    print(f"[narve_calibration] {msg}", file=sys.stderr)


def _curl(url: str, *, retries: int = 3, timeout: int = 30) -> str:
    """GET `url` via curl, returning the body (possibly empty after retries).

    RETRY-ON-EMPTY: this host's IP intermittently returns an empty body on an
    otherwise-successful request, so an empty body is retried (2s sleep) before
    we treat it as truly empty. Never raises on network failure — returns "" so
    the caller degrades to fewer markets rather than crashing the grade."""
    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(
                ["curl", "-s", "-L", "--max-time", str(timeout), "-A", _UA, url],
                capture_output=True,
                text=True,
            )
        except Exception as exc:  # curl missing / OS error
            _log(f"curl raised {exc!r} (attempt {attempt}/{retries})")
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        if proc is not None and proc.returncode != 0:
            _log(f"curl exit {proc.returncode} (attempt {attempt}/{retries})")
        else:
            _log(f"empty body (attempt {attempt}/{retries}) — retrying (NOT 'no data')")
        if attempt < retries:
            time.sleep(2)
    _log(f"no body after {retries} attempts for {url}")
    return ""


def _market_id(market: dict) -> Optional[str]:
    """Stable per-market identifier. conditionId COLLIDES on Polymarket, so we
    prefer the slug, then the numeric id. Returns None if neither is present."""
    slug = market.get("slug")
    if slug:
        return str(slug)
    mid = market.get("id")
    if mid is not None:
        return str(mid)
    return None


def fetch_resolved_markets(max_markets: int = 500) -> list[dict]:
    """Pull decisively-resolved Polymarket markets to use as the answer key.

    Paginates ``GET {_GAMMA}?closed=true&limit=100&offset=N`` until we have
    ``max_markets`` decisively-resolved markets or the feed runs dry. Only
    markets passing the decisive-resolution rule (via ``decisive_outcome`` —
    the {round(a),round(b)}=={0,1} and abs(a-b)>0.9 guard) are kept; ambiguous
    or unresolved ({0,0}, 50/50, etc.) markets are dropped.

    Returns a list of::

        {
          "market_id":        str,    # slug (preferred) or numeric id
          "question":         str,
          "outcomes":         [str, str],   # e.g. ["Yes", "No"]
          "resolved_outcome": int,    # 1 if YES won (price[0] > price[1]), else 0
          "resolved_at":      str | None,   # endDate ISO-8601 when present
        }

    Networked. Degrades gracefully: an empty/failed page ends pagination and we
    return whatever decisive markets we gathered (possibly an empty list).
    """
    out: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while len(out) < max_markets:
        url = f"{_GAMMA}?closed=true&limit={_PAGE}&offset={offset}"
        body = _curl(url)
        if not body.strip():
            _log(f"empty page at offset {offset} — stopping pagination")
            break
        try:
            page = json.loads(body)
        except json.JSONDecodeError as exc:
            _log(f"json parse error at offset {offset}: {exc} — stopping")
            break
        if not isinstance(page, list) or not page:
            _log(f"no more markets at offset {offset} — stopping")
            break

        for market in page:
            if not isinstance(market, dict):
                continue
            verdict = decisive_outcome(
                market.get("outcomePrices"), market.get("outcomes")
            )
            if verdict is None:
                continue  # not decisively resolved
            resolved_outcome, _winning_label = verdict
            mid = _market_id(market)
            if mid is None or mid in seen:
                continue  # unidentifiable or duplicate (gamma can repeat)
            seen.add(mid)
            outcomes = market.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = []
            out.append({
                "market_id": mid,
                "question": str(market.get("question", "")).strip(),
                "outcomes": list(outcomes) if outcomes else [],
                "resolved_outcome": int(resolved_outcome),
                "resolved_at": market.get("endDate"),
            })
            if len(out) >= max_markets:
                break

        offset += _PAGE

    _log(f"gathered {len(out)} decisively-resolved markets")
    return out[:max_markets]


# --------------------------------------------------------------------------- #
# Grading (pure — no network)
# --------------------------------------------------------------------------- #
def _coerce_prob(v: Any) -> Optional[float]:
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= p <= 1.0):
        return None
    return p


def _coerce_outcome(v: Any) -> Optional[int]:
    if v in (1, True):
        return 1
    if v in (0, False):
        return 0
    return None


def grade(forecasts: list[dict]) -> dict:
    """Score narve probabilities against real resolved outcomes.

    Args:
        forecasts: list of
            {"market_id", "narve_probability" (0-1), "resolved_outcome" (1/0 YES)}.
        Records with a missing/out-of-range probability or a non-binary outcome
        are skipped (they aren't gradeable, not silently counted as wrong).

    Returns::

        {
          "n":               int,     # usable forecasts scored
          "accuracy":        float | None,  # directional: (p>=0.5) == (outcome==1)
          "brier":           float | None,  # 0-1, lower is better (None if n<10)
          "calibration":     float | None,  # 0-1 UI score, higher is better
          "reliability_bins": list[dict],   # ten-bin reliability diagram
          "vs_coinflip":     float | None,  # 0.25 - brier; >0 means narve beats 50/50
        }

    Directional accuracy treats p == 0.5 as a YES call (>= 0.5), matching the
    product's "the % is narve's call" framing. Brier and reliability reuse the
    project's single calibration implementation; Brier (hence calibration and
    vs_coinflip) is None below the MIN_SAMPLE=10 threshold where calibration is
    not meaningful, but directional accuracy is always reported when n>=1.
    """
    # Build clean records once: each carries the narve probability and whether
    # narve's directional call was correct (the shape compute_brier_score wants).
    records: list[dict] = []
    correct = 0
    for fc in forecasts or []:
        if not isinstance(fc, dict):
            continue
        p = _coerce_prob(fc.get("narve_probability"))
        outcome = _coerce_outcome(fc.get("resolved_outcome"))
        if p is None or outcome is None:
            continue
        # Directional call: p>=0.5 is a YES prediction.
        narve_says_yes = p >= 0.5
        actual_yes = outcome == 1
        hit = 1 if narve_says_yes == actual_yes else 0
        correct += hit
        records.append({
            # Brier asks: when narve stated probability p that YES happens, did
            # YES happen? So the "stated probability" is narve's YES probability
            # and "resolved_correct" is whether YES actually occurred.
            "predicted_probability_stated": p,
            "resolved_correct": outcome,
        })

    n = len(records)
    if n == 0:
        return {
            "n": 0,
            "accuracy": None,
            "brier": None,
            "calibration": None,
            "reliability_bins": [],
            "vs_coinflip": None,
        }

    accuracy = round(correct / n, 6)
    brier_result = compute_brier_score(records)  # None if n < MIN_SAMPLE (10)
    reliability_bins = reliability_diagram_data(records, bins=10)

    if brier_result is not None:
        brier = brier_result["brier"]
        calibration = brier_result["calibration"]
        vs_coinflip = round(0.25 - brier, 6)
    else:
        brier = None
        calibration = None
        vs_coinflip = None

    return {
        "n": n,
        "accuracy": accuracy,
        "brier": brier,
        "calibration": calibration,
        "reliability_bins": reliability_bins,
        "vs_coinflip": vs_coinflip,
    }
