"""Honesty tests for the walk-forward backtest replay harness.

These tests prove the harness (``gateway/backtest_replay.py``) is *honest*: on a
rigged synthetic fixture with a forecaster who is always right and one who is
always wrong, the credibility-weighted narve strategy must actually win, and the
mechanical no-lookahead guard must actually fire. A backtest that can't pass
these is lying to the pitch deck.

Built against two contracts that exist alongside this file:

1. **Loader** — ``gateway/backtest_dataset.py``::

       load_dataset(path) -> list[dict]      # raises DatasetError on bad input
       parse_iso_date(value) -> datetime.date
       class DatasetError(ValueError)

2. **Harness** — ``gateway/backtest_replay.py`` (HARNESS->REPORT INTERFACE)::

       run_replay(dataset, cold_start=..., edge_threshold=..., ...) -> {
           "decisions": [ { date, market_id, narve_probability,
                            market_price, edge, stake, resolved_correct,
                            <per-source as-of credibility contributions> }, ... ],
           "results":   { roi_pct, win_rate, sharpe, max_drawdown,
                          bet_count, final_bankroll },
       }

The two cold-start methodologies the spec mandates (strict two-window AND
Bayesian-prior) are discovered at runtime rather than hard-coded, so these tests
stay green regardless of the exact identifier strings the harness chose — while
still asserting that *both* documented methods run and return the documented
shape.

This module deliberately does **not** import ``_testdb`` / the FastAPI app / the
DB: the replay harness is pure Python over the JSON fixture. It only puts the
``gateway/`` package root on ``sys.path`` so ``backtest_replay`` and
``backtest_dataset`` import by bare name, matching the rest of the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# ── path bootstrap (no app / no DB) ──────────────────────────────────────────
# ``backtest_replay`` and ``backtest_dataset`` live at the gateway package root,
# two levels up from this test file (gateway/tests/test_*.py). Put that root on
# sys.path so the bare-name imports below resolve the same way the rest of the
# suite resolves ``import db`` etc.
_GATEWAY_ROOT = Path(__file__).resolve().parent.parent
if str(_GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_ROOT))

import backtest_dataset  # noqa: E402  — loader contract
import backtest_replay  # noqa: E402  — harness contract


FIXTURE_PATH = _GATEWAY_ROOT / "data" / "backtest" / "synthetic_fixture.json"

# Handles whose behaviour is rigged in the synthetic fixture (see its
# "description" field). The oracle is on the correct side of every market; the
# clueless source is on the wrong side of every market.
ORACLE = "@oracle_always_right"
CLUELESS = "@clueless_always_wrong"

# Documented result keys (HARNESS->REPORT INTERFACE).
RESULT_KEYS = {
    "roi_pct",
    "win_rate",
    "sharpe",
    "max_drawdown",
    "bet_count",
    "final_bankroll",
}
# Documented per-decision keys (the per-source credibility contributions are an
# additional nested/aggregated field whose exact name we don't pin here).
DECISION_KEYS = {
    "date",
    "market_id",
    "narve_probability",
    "market_price",
    "edge",
    "stake",
    "resolved_correct",
}


# ── cold-start method discovery ──────────────────────────────────────────────


def _cold_start_methods() -> list:
    """Return the two cold-start identifiers the harness accepts.

    Prefers an explicit, machine-readable advertisement from the harness so the
    test never guesses:
      * a module-level ``COLD_START_METHODS`` list/tuple, or
      * a ``cold_start_methods()`` callable.
    Falls back to the two names the spec mandates (strict two-window AND
    Bayesian-prior) under their most common spellings.
    """
    advertised = getattr(backtest_replay, "COLD_START_METHODS", None)
    if callable(advertised):
        advertised = advertised()
    if advertised is None:
        fn = getattr(backtest_replay, "cold_start_methods", None)
        if callable(fn):
            advertised = fn()
    if advertised:
        methods = list(advertised)
        assert len(methods) >= 2, (
            "spec mandates BOTH cold-start methodologies (strict two-window AND "
            f"Bayesian-prior); harness advertises only: {methods!r}"
        )
        return methods
    # Fallback: the two documented methodologies.
    return ["two_window", "bayesian"]


def _run(cold_start, *, edge_threshold=0.05):
    """Call run_replay with the documented args, loading the fixture fresh.

    Loads a fresh dataset per call so a harness that mutates its input can't
    leak state between cold-start runs.
    """
    dataset = backtest_dataset.load_dataset(FIXTURE_PATH)
    return backtest_replay.run_replay(
        dataset,
        cold_start=cold_start,
        edge_threshold=edge_threshold,
    )


def _assert_documented_shape(out, *, where: str) -> None:
    """Assert ``out`` matches the documented {decisions, results} shape."""
    assert isinstance(out, dict), f"{where}: run_replay must return a dict, got {type(out).__name__}"
    assert "decisions" in out, f"{where}: missing 'decisions' key"
    assert "results" in out, f"{where}: missing 'results' key"

    decisions = out["decisions"]
    results = out["results"]

    assert isinstance(decisions, list), f"{where}: 'decisions' must be a list, got {type(decisions).__name__}"
    assert isinstance(results, dict), f"{where}: 'results' must be a dict, got {type(results).__name__}"

    missing = RESULT_KEYS - set(results)
    assert not missing, f"{where}: results missing documented keys: {sorted(missing)}"

    for i, dec in enumerate(decisions):
        assert isinstance(dec, dict), f"{where}: decisions[{i}] must be a dict, got {type(dec).__name__}"
        dmissing = DECISION_KEYS - set(dec)
        assert not dmissing, f"{where}: decisions[{i}] missing documented keys: {sorted(dmissing)}"


def _decision_handles(decision: dict) -> str:
    """Flatten a decision's per-source credibility contributions to a string.

    The exact container for "per-source as-of credibility contributions" isn't
    pinned by the interface (could be a dict, a list of dicts, nested under a
    key). We just need to detect which source handles appear, so we serialise
    the whole decision and substring-match handles against it.
    """
    return repr(decision)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def dataset():
    return backtest_dataset.load_dataset(FIXTURE_PATH)


@pytest.fixture(scope="module")
def cold_start_methods():
    return _cold_start_methods()


# ─────────────────────────────────────────────────────────────────────────────
# KNOWN-ANSWER: the harness must not lie on a rigged fixture.
# ─────────────────────────────────────────────────────────────────────────────


def test_fixture_loads_and_is_rigged(dataset):
    """Sanity: the synthetic fixture loads and contains the rigged handles.

    Guards the rest of the suite — if the fixture ever stops being the
    known-answer rig, the honesty assertions below would be meaningless.
    """
    assert len(dataset) >= 4, "expected the 4-market synthetic fixture"
    handles = {
        fc["source_handle"]
        for m in dataset
        for fc in m["forecasts"]
    }
    assert ORACLE in handles, f"fixture must contain the always-right oracle {ORACLE!r}"
    assert CLUELESS in handles, f"fixture must contain the always-wrong source {CLUELESS!r}"

    # Cross-check the known-answer invariant the foundation guarantees: the
    # oracle is on the correct side of every market, the clueless source on the
    # wrong side. (>0.5 means it believes YES.)
    for m in dataset:
        outcome = m["resolved_outcome"]
        for fc in m["forecasts"]:
            p = fc["predicted_probability"]
            if fc["source_handle"] == ORACLE:
                believes_yes = p > 0.5
                assert believes_yes == bool(outcome), (
                    f"oracle should be on the correct side of {m['market_id']} "
                    f"(outcome={outcome}, p={p})"
                )
            elif fc["source_handle"] == CLUELESS:
                believes_yes = p > 0.5
                assert believes_yes != bool(outcome), (
                    f"clueless should be on the wrong side of {m['market_id']} "
                    f"(outcome={outcome}, p={p})"
                )


def test_known_answer_narve_wins_and_credibility_separates(cold_start_methods):
    """The core honesty test.

    On the rigged fixture, under each cold-start methodology:
      * the narve strategy WINS — positive ROI and a high win rate;
      * the always-right oracle ends up trusted (high as-of credibility);
      * the always-wrong source ends up distrusted (low as-of credibility).

    If the harness scored a clueless source as credible, or lost money betting
    alongside a perfect oracle against a mispriced market, it would be lying —
    these assertions catch that.
    """
    for cold_start in cold_start_methods:
        out = _run(cold_start)
        _assert_documented_shape(out, where=f"cold_start={cold_start!r}")
        results = out["results"]
        decisions = out["decisions"]

        # The harness must actually place bets to prove anything.
        assert results["bet_count"] >= 1, (
            f"cold_start={cold_start!r}: harness placed no bets on the rigged "
            f"fixture — nothing was proven"
        )

        # narve WINS: positive ROI and a winning record.
        assert results["roi_pct"] > 0, (
            f"cold_start={cold_start!r}: narve must be profitable on a fixture "
            f"with a perfect oracle vs. a mispriced market, got roi_pct="
            f"{results['roi_pct']}"
        )
        assert results["win_rate"] > 0.5, (
            f"cold_start={cold_start!r}: narve must win the majority of bets on "
            f"the rigged fixture, got win_rate={results['win_rate']}"
        )

        # final_bankroll must corroborate a positive ROI (no contradictory
        # bookkeeping between the two reported numbers).
        assert results["final_bankroll"] > 0, (
            f"cold_start={cold_start!r}: final_bankroll must be positive"
        )

        # Credibility separation: the oracle must end MORE trusted than the
        # clueless source. We read the as-of credibility contributions logged on
        # the last decision that mentions each handle.
        oracle_cred = _latest_credibility(decisions, ORACLE)
        clueless_cred = _latest_credibility(decisions, CLUELESS)
        assert oracle_cred is not None, (
            f"cold_start={cold_start!r}: oracle never appeared in any decision's "
            f"per-source credibility contributions"
        )
        if clueless_cred is not None:
            assert oracle_cred > clueless_cred, (
                f"cold_start={cold_start!r}: always-right oracle ({oracle_cred}) "
                f"must end MORE credible than always-wrong source "
                f"({clueless_cred})"
            )
        # The oracle, once unlocked, should be strongly trusted (well above the
        # uninformed 0.5 prior). We allow a band rather than demanding exactly
        # 1.0 because Bayesian smoothing pulls toward the prior on small N.
        assert oracle_cred > 0.5, (
            f"cold_start={cold_start!r}: always-right oracle should be trusted "
            f"above the 0.5 prior, got {oracle_cred}"
        )


def test_known_answer_narve_beats_follow_market_baseline(cold_start_methods):
    """narve must BEAT the follow-market baseline on the rigged fixture.

    A number with no baseline is meaningless (spec §Decisions). The follow-market
    baseline just trusts the market price; on a fixture where the market is
    systematically mispriced and a perfect oracle exists, the credibility-weighted
    narve strategy must do strictly better than blindly following the market.

    The follow-market baseline ROI is sourced, in priority order, from:
      1. a documented baseline in the harness output
         (``out["baselines"]["follow_market"]["roi_pct"]`` or similar), else
      2. a harness helper ``follow_market_baseline(dataset, ...)``, else
      3. the test computes it directly from the fixture as a last resort.
    Whichever path, narve's ROI must exceed it.
    """
    for cold_start in cold_start_methods:
        out = _run(cold_start)
        narve_roi = out["results"]["roi_pct"]

        baseline_roi = _follow_market_roi_from_output(out)
        if baseline_roi is None:
            baseline_roi = _follow_market_roi_via_helper()
        if baseline_roi is None:
            baseline_roi = _compute_follow_market_roi_from_fixture()

        assert baseline_roi is not None, (
            "could not obtain a follow-market baseline ROI from the harness "
            "output, a harness helper, or the fixture"
        )
        assert narve_roi > baseline_roi, (
            f"cold_start={cold_start!r}: narve ROI ({narve_roi}) must BEAT the "
            f"follow-market baseline ROI ({baseline_roi}) on the rigged fixture"
        )


# ─────────────────────────────────────────────────────────────────────────────
# LOOKAHEAD GUARD: the mechanical integrity check must fire.
# ─────────────────────────────────────────────────────────────────────────────


def test_lookahead_forecast_after_decision_raises(dataset):
    """A forecast dated AFTER its market's resolution must make the harness raise.

    The spec's headline integrity guard: "harness hard-fails if any input
    timestamp >= the decision date." We poison a copy of the fixture so one
    forecast is made on/after ``resolved_at`` — the bug that silently inflates
    every backtest — and assert the run blows up loudly rather than scoring it.

    Accepts either the loader's ``DatasetError`` (if the harness re-validates via
    the loader) or any explicit exception the harness raises in its own
    per-decision assertion. What must NOT happen is a clean, lying result.
    """
    import copy

    poisoned = copy.deepcopy(dataset)
    target = poisoned[0]
    resolved_at = target["resolved_at"]
    # Move a forecast to the resolution date (>= decision date == lookahead).
    target["forecasts"][0]["made_at"] = resolved_at

    methods = _cold_start_methods()
    with pytest.raises(Exception) as excinfo:
        backtest_replay.run_replay(
            poisoned,
            cold_start=methods[0],
            edge_threshold=0.05,
        )
    # It must be a real integrity failure, not an incidental KeyError/TypeError
    # that happens to abort. We accept DatasetError or any error whose message
    # mentions lookahead / the offending timestamp relationship.
    err = excinfo.value
    msg = str(err).lower()
    is_dataset_error = isinstance(err, backtest_dataset.DatasetError)
    mentions_lookahead = (
        "lookahead" in msg
        or "look-ahead" in msg
        or "made_at" in msg
        or "resolved_at" in msg
        or "before" in msg
        or "after" in msg
        or "decision date" in msg
    )
    assert is_dataset_error or mentions_lookahead, (
        f"lookahead poison must raise an integrity error naming the violation; "
        f"got {type(err).__name__}: {err!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SHAPE: both cold-start methods run and return the documented interface.
# ─────────────────────────────────────────────────────────────────────────────


def test_both_cold_start_methods_run_and_return_documented_shape(cold_start_methods):
    """Both mandated cold-start methodologies execute without error and return
    the documented {decisions, results} shape with all documented keys."""
    assert len(cold_start_methods) >= 2, (
        "spec mandates BOTH cold-start methodologies (strict two-window AND "
        f"Bayesian-prior); got {cold_start_methods!r}"
    )
    for cold_start in cold_start_methods:
        out = _run(cold_start)
        _assert_documented_shape(out, where=f"cold_start={cold_start!r}")

        # Decision fields must be of sane types where the contract is explicit.
        for i, dec in enumerate(out["decisions"]):
            where = f"cold_start={cold_start!r} decisions[{i}]"
            assert isinstance(dec["market_id"], str), f"{where}: market_id must be str"
            assert isinstance(dec["narve_probability"], (int, float)), (
                f"{where}: narve_probability must be numeric"
            )
            assert isinstance(dec["market_price"], (int, float)), (
                f"{where}: market_price must be numeric"
            )
            assert isinstance(dec["edge"], (int, float)), f"{where}: edge must be numeric"
            assert isinstance(dec["stake"], (int, float)), f"{where}: stake must be numeric"
            assert isinstance(dec["resolved_correct"], (bool, int)), (
                f"{where}: resolved_correct must be bool/int"
            )


# ─────────────────────────────────────────────────────────────────────────────
# LOADER VALIDATION: a malformed record raises a clear error.
# ─────────────────────────────────────────────────────────────────────────────


def test_loader_rejects_malformed_record(tmp_path):
    """A malformed dataset record must raise ``DatasetError`` with a located,
    human-readable message (not a bare KeyError / ValueError)."""
    import json

    bad = {
        "markets": [
            {
                "market_id": "bad-market-1",
                "question": "[BAD] missing required forecast field",
                "resolved_outcome": 1,
                "resolved_at": "2026-11-04",
                "price_timeline": [{"date": "2026-10-01", "yes_price": 0.5}],
                "forecasts": [
                    {
                        "source_handle": "@x",
                        # predicted_probability deliberately omitted
                        "made_at": "2026-10-01",
                        "url": "",
                    }
                ],
            }
        ]
    }
    p = tmp_path / "malformed.json"
    p.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(backtest_dataset.DatasetError) as excinfo:
        backtest_dataset.load_dataset(p)
    msg = str(excinfo.value)
    # Message must locate the offending record and name the missing field.
    assert "bad-market-1" in msg or "market[0]" in msg, (
        f"DatasetError must locate the offending record, got: {msg!r}"
    )
    assert "predicted_probability" in msg, (
        f"DatasetError must name the missing field, got: {msg!r}"
    )


def test_loader_rejects_dataset_level_lookahead(tmp_path):
    """The loader's own no-lookahead invariant: a forecast made on/after
    ``resolved_at`` is rejected at load time with a clear error."""
    import json

    bad = {
        "markets": [
            {
                "market_id": "lookahead-market-1",
                "question": "[BAD] forecast not strictly before resolution",
                "resolved_outcome": 1,
                "resolved_at": "2026-11-04",
                "price_timeline": [{"date": "2026-10-01", "yes_price": 0.5}],
                "forecasts": [
                    {
                        "source_handle": "@x",
                        "predicted_probability": 0.7,
                        "made_at": "2026-11-04",  # == resolved_at → lookahead
                        "url": "",
                    }
                ],
            }
        ]
    }
    p = tmp_path / "lookahead.json"
    p.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(backtest_dataset.DatasetError) as excinfo:
        backtest_dataset.load_dataset(p)
    msg = str(excinfo.value).lower()
    assert "lookahead" in msg or "before" in msg or "made_at" in msg, (
        f"loader must flag the dataset-level lookahead clearly, got: {msg!r}"
    )


# ── helpers used by the known-answer tests ───────────────────────────────────


def _latest_credibility(decisions: list, handle: str):
    """Best-effort extraction of a handle's as-of credibility from decisions.

    The interface says each decision logs "per-source as-of credibility
    contributions" but does not pin the container shape. We walk decisions in
    reverse (most recent as-of credibility wins) and pull the first numeric
    value we can associate with ``handle`` from common shapes:
      * dec["credibilities"][handle] / dec["source_credibility"][handle]
      * a list of {source_handle/handle/source, credibility/cred/weight} dicts
        under any decision key
    Returns a float, or None if the handle never appears.
    """
    cred_keys = ("credibility", "cred", "as_of_credibility", "weight", "contribution")
    handle_keys = ("source_handle", "handle", "source", "name")

    for dec in reversed(decisions):
        for value in dec.values():
            # Shape A: dict mapping handle -> number (or handle -> {cred:...}).
            if isinstance(value, dict):
                if handle in value:
                    inner = value[handle]
                    num = _coerce_cred(inner, cred_keys)
                    if num is not None:
                        return num
            # Shape B: list of per-source dicts.
            elif isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    item_handle = None
                    for hk in handle_keys:
                        if hk in item:
                            item_handle = item[hk]
                            break
                    if item_handle != handle:
                        continue
                    num = _coerce_cred(item, cred_keys)
                    if num is not None:
                        return num
    return None


def _coerce_cred(obj, cred_keys):
    """Pull a numeric credibility out of ``obj`` (a number, or a dict)."""
    if isinstance(obj, bool):
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, dict):
        for ck in cred_keys:
            if ck in obj and isinstance(obj[ck], (int, float)) and not isinstance(obj[ck], bool):
                return float(obj[ck])
    return None


def _follow_market_roi_from_output(out: dict):
    """Pull a follow-market baseline ROI from the harness output if present."""
    baselines = out.get("baselines") or out.get("results", {}).get("baselines")
    if not isinstance(baselines, dict):
        return None
    for key in ("follow_market", "always_follow_market", "market", "follow-market"):
        b = baselines.get(key)
        if isinstance(b, dict) and isinstance(b.get("roi_pct"), (int, float)):
            return float(b["roi_pct"])
        if isinstance(b, (int, float)) and not isinstance(b, bool):
            return float(b)
    return None


def _follow_market_roi_via_helper():
    """Call a harness-provided follow-market baseline helper if it exists."""
    dataset = backtest_dataset.load_dataset(FIXTURE_PATH)
    for name in ("follow_market_baseline", "baseline_follow_market", "follow_market"):
        fn = getattr(backtest_replay, name, None)
        if callable(fn):
            try:
                res = fn(dataset)
            except TypeError:
                continue
            if isinstance(res, dict) and isinstance(res.get("roi_pct"), (int, float)):
                return float(res["roi_pct"])
            if isinstance(res, dict) and isinstance(res.get("results"), dict):
                roi = res["results"].get("roi_pct")
                if isinstance(roi, (int, float)) and not isinstance(roi, bool):
                    return float(roi)
            if isinstance(res, (int, float)) and not isinstance(res, bool):
                return float(res)
    return None


def _compute_follow_market_roi_from_fixture():
    """Last-resort follow-market baseline computed directly from the fixture.

    The follow-market strategy trusts the market: it would not bet against the
    price (no edge over the market by construction), so its ROI is 0.0. On the
    rigged fixture narve must beat that, which is exactly the bar we want — a
    profitable narve strategy must clear a do-nothing/market-trusting baseline.
    """
    # A market-trusting strategy finds no edge over the market and stays flat.
    return 0.0
