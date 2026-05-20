"""Tests for the multi-model ensemble.

The tests inject a fake Anthropic client so we never touch the network.
Each fake produces a deterministic recommendation per model so the
agreement logic can be exercised end-to-end.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import insight_ensemble as iens


# ─── Fake SDK plumbing (re-used from test_insight.py pattern) ────────────────

class _FakeTextDelta:
    type = "text_delta"

    def __init__(self, text):
        self.text = text


class _FakeContentBlockDeltaEvent:
    type = "content_block_delta"

    def __init__(self, text):
        self.delta = _FakeTextDelta(text)


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeUsage:
    def __init__(self):
        self.input_tokens = 5000
        self.output_tokens = 200
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 4800


class _FakeFinalMessage:
    def __init__(self, json_text, model_id):
        self.content = [_FakeTextBlock(json_text)]
        self.usage = _FakeUsage()
        self.model = model_id
        self.stop_reason = "end_turn"


class _FakeStream:
    def __init__(self, json_text, model_id, raise_in_iter=False):
        self._json = json_text
        self._model = model_id
        self._raise = raise_in_iter

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        if self._raise:
            raise RuntimeError("simulated network blip")
        yield _FakeContentBlockDeltaEvent(self._json)

    def get_final_message(self):
        return _FakeFinalMessage(self._json, self._model)


class _FakeMessagesAPI:
    def __init__(self, per_model_responses):
        # per_model_responses[model_id] = (json_str, raise_in_iter)
        self._responses = per_model_responses
        self.calls = []

    def stream(self, **kwargs):
        model = kwargs["model"]
        self.calls.append(model)
        json_str, raise_in_iter = self._responses.get(
            model, (None, False),
        )
        if json_str is None:
            # Model not configured → simulate an empty stream
            return _FakeStream("{}", model, raise_in_iter=True)
        return _FakeStream(json_str, model, raise_in_iter=raise_in_iter)


class _FakeClient:
    def __init__(self, per_model_responses):
        self.messages = _FakeMessagesAPI(per_model_responses)


def _insight_json(recommendation="BUY_YES", confidence="high"):
    return json.dumps({
        "recommendation": recommendation,
        "confidence": confidence,
        "headline": f"{recommendation} ({confidence})",
        "key_facts": ["fact"],
        "key_risks": ["risk"],
        "suggested_limit_cents": 60,
        "tail_warning": False,
        "disclaimer": "Not investment advice.",
    })


# ─── run_ensemble ─────────────────────────────────────────────────────────────

def test_ensemble_unanimous_buy_yes():
    """All three models agree on BUY_YES + high confidence."""
    responses = {
        m: (_insight_json("BUY_YES", "high"), False)
        for m in iens.ENSEMBLE_MODELS
    }
    client = _FakeClient(responses)
    out = iens.run_ensemble({"market_id": "m1"}, client=client)
    assert out["n_complete"] == 3
    assert out["n_failed"] == 0
    assert out["agreement"]["level"] == "unanimous"
    assert out["agreement"]["majority"] == "BUY_YES"
    assert out["agreement"]["unanimous_confidence"] == "high"


def test_ensemble_majority_with_one_dissent():
    """Haiku + Sonnet say BUY_YES; Opus says PASS — majority of 2/3."""
    responses = {
        iens.ENSEMBLE_MODELS[0]: (_insight_json("BUY_YES"), False),
        iens.ENSEMBLE_MODELS[1]: (_insight_json("BUY_YES"), False),
        iens.ENSEMBLE_MODELS[2]: (_insight_json("PASS"), False),
    }
    client = _FakeClient(responses)
    out = iens.run_ensemble({"market_id": "m1"}, client=client)
    assert out["agreement"]["level"] == "majority"
    assert out["agreement"]["majority"] == "BUY_YES"
    assert out["agreement"]["majority_count"] == 2


def test_ensemble_split():
    """Three models, three different recommendations → split."""
    responses = {
        iens.ENSEMBLE_MODELS[0]: (_insight_json("BUY_YES"), False),
        iens.ENSEMBLE_MODELS[1]: (_insight_json("BUY_NO"), False),
        iens.ENSEMBLE_MODELS[2]: (_insight_json("PASS"), False),
    }
    client = _FakeClient(responses)
    out = iens.run_ensemble({"market_id": "m1"}, client=client)
    assert out["agreement"]["level"] == "split"
    assert out["agreement"]["majority"] is None


def test_ensemble_partial_failure_returns_remaining_members():
    """Opus errors mid-stream; Haiku + Sonnet should still complete."""
    responses = {
        iens.ENSEMBLE_MODELS[0]: (_insight_json("BUY_YES"), False),
        iens.ENSEMBLE_MODELS[1]: (_insight_json("BUY_YES"), False),
        iens.ENSEMBLE_MODELS[2]: ("", True),  # raises in iter
    }
    client = _FakeClient(responses)
    out = iens.run_ensemble({"market_id": "m1"}, client=client)
    assert out["n_complete"] == 2
    assert out["n_failed"] == 1
    # Even with one failed member, majority of 2/2 is still unanimous
    assert out["agreement"]["level"] == "unanimous"
    # The failed member surfaces an error field
    failed = [m for m in out["members"] if m["insight"] is None]
    assert len(failed) == 1
    assert failed[0]["error"] is not None


def test_ensemble_returns_members_in_canonical_order():
    """Whichever model finishes first, members are sorted into the
    ENSEMBLE_MODELS order so the frontend's three-column layout is
    stable."""
    responses = {
        m: (_insight_json(), False) for m in iens.ENSEMBLE_MODELS
    }
    client = _FakeClient(responses)
    out = iens.run_ensemble({"market_id": "m1"}, client=client)
    assert [m["model"] for m in out["members"]] == list(iens.ENSEMBLE_MODELS)


def test_ensemble_all_fail_returns_no_data():
    responses = {m: ("", True) for m in iens.ENSEMBLE_MODELS}
    client = _FakeClient(responses)
    out = iens.run_ensemble({"market_id": "m1"}, client=client)
    assert out["n_complete"] == 0
    assert out["n_failed"] == 3
    assert out["agreement"]["level"] == "no_data"


def test_ensemble_no_models_returns_error():
    out = iens.run_ensemble({"market_id": "m1"}, models=())
    assert out["n_complete"] == 0
    assert out["error"] == "no models specified"


def test_ensemble_two_model_unanimous():
    """Custom 2-model ensemble with both agreeing."""
    custom = ("claude-haiku-4-5", "claude-sonnet-4-6")
    responses = {m: (_insight_json("BUY_NO"), False) for m in custom}
    client = _FakeClient(responses)
    out = iens.run_ensemble({"market_id": "m1"}, models=custom, client=client)
    assert out["agreement"]["level"] == "unanimous"
    assert out["agreement"]["majority"] == "BUY_NO"


def test_ensemble_unanimous_rec_split_confidence():
    """All three agree on recommendation but disagree on confidence.
    `unanimous_confidence` should be None in that case."""
    responses = {
        iens.ENSEMBLE_MODELS[0]: (_insight_json("BUY_YES", "high"), False),
        iens.ENSEMBLE_MODELS[1]: (_insight_json("BUY_YES", "medium"), False),
        iens.ENSEMBLE_MODELS[2]: (_insight_json("BUY_YES", "low"), False),
    }
    client = _FakeClient(responses)
    out = iens.run_ensemble({"market_id": "m1"}, client=client)
    assert out["agreement"]["level"] == "unanimous"
    assert out["agreement"]["unanimous_confidence"] is None
