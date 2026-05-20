"""Tests for the LLM actionable-insight engine.

The Anthropic SDK call is mocked end-to-end — these tests never hit
the live API, but they do verify:

  * The output JSON schema is well-formed and only uses constructs
    structured outputs supports (no min/maxLength, no recursion, all
    objects have additionalProperties:false).
  * The system prompt is large enough to clear the Haiku 4.5 cache
    minimum (4096 tokens, approximated by 4 chars/token).
  * The system block has `cache_control: ephemeral` so prompt caching
    actually kicks in.
  * The streaming wrapper turns SDK events into StreamChunks correctly,
    including a final `complete` with parsed JSON + usage.
  * Errors mid-stream surface as `error` chunks, not exceptions.
  * The model selector coerces unknown values to MODEL_FAST.
  * Context digesters trim ensemble lists and trajectory tails.
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock

import pytest

import insight


# ─── Schema sanity ────────────────────────────────────────────────────────────

def test_output_schema_uses_supported_constructs_only():
    """structured outputs reject minLength/maxLength/minimum/maximum/
    multipleOf and additionalProperties != false. Walk the schema and
    confirm we don't ship any of those."""
    def walk(node):
        if isinstance(node, dict):
            if "minLength" in node or "maxLength" in node:
                raise AssertionError(f"string-length constraint in schema: {node}")
            if "minimum" in node or "maximum" in node or "multipleOf" in node:
                raise AssertionError(f"numeric constraint in schema: {node}")
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, \
                    f"object missing additionalProperties:false: {node}"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(insight.OUTPUT_SCHEMA)


def test_output_schema_has_all_required_fields():
    """Every property must appear in `required` so the model can't
    silently omit fields the frontend depends on."""
    props = insight.OUTPUT_SCHEMA["properties"]
    required = set(insight.OUTPUT_SCHEMA["required"])
    assert required == set(props.keys()), \
        f"required ({required}) != properties ({set(props.keys())})"


def test_recommendation_enum_is_exhaustive():
    enum = insight.OUTPUT_SCHEMA["properties"]["recommendation"]["enum"]
    assert set(enum) == {"BUY_YES", "BUY_NO", "PASS", "WAIT_AND_SEE"}


def test_confidence_enum_is_three_tier():
    enum = insight.OUTPUT_SCHEMA["properties"]["confidence"]["enum"]
    assert enum == ["high", "medium", "low"]


# ─── System prompt ────────────────────────────────────────────────────────────

def test_system_prompt_exceeds_haiku_cache_minimum():
    """Haiku 4.5 minimum cacheable prefix is 4096 tokens. Rough estimate
    is 4 chars/token — confirm the prompt clears that bar with margin,
    or caching will silently no-op."""
    char_count = len(insight._SYSTEM_PROMPT)
    estimated_tokens = char_count // 4
    assert estimated_tokens >= 4096, \
        f"system prompt ~{estimated_tokens} tokens; min for Haiku 4.5 is 4096"


def test_system_blocks_have_cache_control():
    """Without cache_control on the last block, nothing caches."""
    blocks = insight._system_blocks()
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_system_prompt_includes_station_list():
    """If the canonical city keys aren't in the system prompt, the
    model will invent ones — and the cached prefix changes when we
    inject the list per-call, breaking the cache."""
    p = insight._SYSTEM_PROMPT
    for city in ("new york", "chicago", "london", "paris", "tokyo", "sydney"):
        assert city in p, f"station list missing {city!r}"


def test_system_prompt_documents_all_recommendation_outcomes():
    """Each enum value should appear in the decision-logic section."""
    p = insight._SYSTEM_PROMPT
    for val in ("BUY_YES", "BUY_NO", "PASS", "WAIT_AND_SEE"):
        assert val in p, f"recommendation {val!r} not documented in prompt"


# ─── User message ─────────────────────────────────────────────────────────────

def test_user_message_serializes_context_deterministically():
    """Same context in, same string out — required for prompt caching of
    the user turn (when we ever add it) and for deterministic tests."""
    ctx = {"market_id": "m1", "city": "nyc", "yes_price": 0.62}
    a = insight.build_user_message(ctx)
    b = insight.build_user_message(ctx)
    assert a == b
    # And key order in the source dict doesn't matter
    c = insight.build_user_message({"yes_price": 0.62, "market_id": "m1", "city": "nyc"})
    assert a == c


# ─── Context digesters ────────────────────────────────────────────────────────

def test_digest_forecast_drops_full_ensemble_keeps_percentiles():
    fc = {
        "mean": 75.0, "std": 3.0, "raw_mean": 74.0, "raw_std": 2.5,
        "min": 65.0, "max": 85.0,
        "ensemble": list(range(60, 90)),  # 30 members
        "n_highres": 1, "highres_models": ["gfs_hrrr"],
        "lead_time_mult": 1.2, "bias_corrected": True, "n_bias_models": 6,
        "empirical_sigma_floor": 2.8,
        "downscaling": {"applied": True, "delta_f": -0.5, "r2": 0.72, "n": 90},
        "source": "8 models + NWS + climo + bias-corrected",
    }
    d = insight.digest_forecast(fc)
    assert d["n_members"] == 30
    assert "ensemble" not in d  # full list dropped
    assert d["percentiles"]["p50"] == pytest.approx(74.5, abs=0.5)
    # range(60, 90) has 30 values 60..89; p05/p95 with linear interp
    # land at ~61.45 / ~87.55, rounded to one decimal by the helper.
    assert d["percentiles"]["p05"] == pytest.approx(61.4, abs=0.2)
    assert d["percentiles"]["p95"] == pytest.approx(87.5, abs=0.2)
    assert d["downscaling"]["applied"] is True


def test_digest_forecast_handles_none():
    assert insight.digest_forecast(None) is None


def test_digest_forecast_handles_no_ensemble_members():
    d = insight.digest_forecast({"mean": 75.0, "std": 3.0, "ensemble": []})
    assert d["n_members"] == 0
    assert d["percentiles"] == {}


def test_digest_intraday_truncates_trajectory():
    traj = [{"obs_time": f"2026-05-07T{h:02d}:00:00Z", "temp_f": 60.0 + h}
            for h in range(20)]
    d = insight.digest_intraday({"running_max": 76.5, "obs_count": 20}, traj)
    assert len(d["trajectory"]) == 12  # capped at 12 most-recent
    # Newest entries kept (the trajectory is passed newest-last)
    assert d["trajectory"][-1]["temp_f"] == 79.0


def test_digest_intraday_returns_none_when_both_absent():
    assert insight.digest_intraday(None, None) is None


def test_digest_intraday_works_with_only_running():
    d = insight.digest_intraday({"running_max": 76.5, "obs_count": 18}, None)
    assert d["running_max"] == 76.5
    assert d["trajectory"] == []


# ─── assemble_context ─────────────────────────────────────────────────────────

def test_assemble_context_computes_edge_from_prob_breakdown():
    market = {"market_id": "m1", "city": "nyc", "yes_price": 0.6,
              "question": "NYC above 75°F?", "target_date": "2026-05-07"}
    temp_info = {"threshold": 75.0, "is_over": True, "unit": "F"}
    prob = {"probability": 0.78, "gaussian": 0.75, "empirical": 0.80,
            "method": "empirical", "tail_warning": False,
            "intraday_conditional": None}
    ctx = insight.assemble_context(
        market=market, forecast=None, temp_info=temp_info,
        model_prob_breakdown=prob,
    )
    assert ctx["model_prob"] == 0.78
    assert ctx["edge"] == pytest.approx(0.18, abs=0.001)
    assert ctx["no_price"] == pytest.approx(0.4, abs=0.001)
    assert ctx["model_prob_empirical"] == 0.80
    assert ctx["tail_warning"] is False


def test_assemble_context_falls_back_to_market_model_prob():
    """When no prob breakdown is supplied, use whatever the market dict
    already carried (e.g. an older snapshot)."""
    market = {"market_id": "m1", "yes_price": 0.5, "model_prob": 0.6}
    temp_info = {}
    ctx = insight.assemble_context(market=market, forecast=None,
                                   temp_info=temp_info)
    assert ctx["model_prob"] == 0.6
    assert ctx["edge"] == pytest.approx(0.1)


# ─── Streaming wrapper (mocked) ───────────────────────────────────────────────

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
    def __init__(self, in_tok=12000, out_tok=300,
                 cache_creation=0, cache_read=0):
        self.input_tokens = in_tok
        self.output_tokens = out_tok
        self.cache_creation_input_tokens = cache_creation
        self.cache_read_input_tokens = cache_read


class _FakeFinalMessage:
    def __init__(self, json_text, **usage_kwargs):
        self.content = [_FakeTextBlock(json_text)]
        self.usage = _FakeUsage(**usage_kwargs)
        self.model = "claude-haiku-4-5"
        self.stop_reason = "end_turn"


class _FakeStream:
    """Minimal stand-in for the SDK stream context manager. Yields the
    canned deltas, then returns the full message via get_final_message()."""
    def __init__(self, deltas, final_message):
        self._deltas = deltas
        self._final = final_message
        self.captured_params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for d in self._deltas:
            yield d

    def get_final_message(self):
        return self._final


class _FakeMessagesAPI:
    def __init__(self, stream):
        self._stream = stream
        self.last_kwargs = None

    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        return self._stream


class _FakeClient:
    def __init__(self, stream):
        self.messages = _FakeMessagesAPI(stream)


def _build_canned_stream(final_json: dict, deltas: list[str]):
    delta_events = [_FakeContentBlockDeltaEvent(d) for d in deltas]
    final = _FakeFinalMessage(json.dumps(final_json),
                              cache_creation=5200, cache_read=0)
    return _FakeStream(delta_events, final)


def test_stream_insight_yields_tokens_then_complete():
    out_json = {
        "recommendation": "BUY_YES", "confidence": "high",
        "headline": "Buy YES at 66¢", "key_facts": ["edge +22pp"],
        "key_risks": ["station ambiguity"], "suggested_limit_cents": 66,
        "tail_warning": False, "disclaimer": "Not investment advice.",
    }
    stream = _build_canned_stream(out_json, deltas=['{"recommendation":', '"BUY_YES"', "..."])
    client = _FakeClient(stream)

    chunks = list(insight.stream_insight({"market_id": "m1"}, client=client))
    types = [c.type for c in chunks]
    assert types == ["token", "token", "token", "complete"]

    # Each token chunk forwards the text verbatim
    assert chunks[0].data == {"text": '{"recommendation":'}
    assert chunks[1].data == {"text": '"BUY_YES"'}

    # Final chunk has parsed insight + usage
    complete = chunks[-1].data
    assert complete["insight"]["recommendation"] == "BUY_YES"
    assert complete["insight"]["headline"] == "Buy YES at 66¢"
    assert complete["usage"]["input_tokens"] == 12000
    assert complete["usage"]["cache_creation_input_tokens"] == 5200
    assert complete["model"] == "claude-haiku-4-5"


def test_stream_insight_passes_cache_control_to_sdk():
    """The whole point of the system prompt — verify the SDK call gets
    a system block with cache_control set."""
    stream = _build_canned_stream(
        {"recommendation": "PASS", "confidence": "low",
         "headline": "Pass", "key_facts": ["thin"], "key_risks": ["small"],
         "suggested_limit_cents": None, "tail_warning": False,
         "disclaimer": "Not investment advice."},
        deltas=["{}"],
    )
    client = _FakeClient(stream)
    list(insight.stream_insight({"market_id": "m1"}, client=client))
    kwargs = client.messages.last_kwargs
    assert kwargs["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["model"] == insight.MODEL_FAST


def test_stream_insight_coerces_unknown_model_to_fast():
    """A stray ?model=opus shouldn't pick an arbitrary expensive model."""
    stream = _build_canned_stream(
        {"recommendation": "PASS", "confidence": "low", "headline": "x",
         "key_facts": ["x"], "key_risks": ["x"], "suggested_limit_cents": None,
         "tail_warning": False, "disclaimer": "Not investment advice."},
        deltas=["{}"],
    )
    client = _FakeClient(stream)
    list(insight.stream_insight({"market_id": "m1"}, model="opus-4-7", client=client))
    assert client.messages.last_kwargs["model"] == insight.MODEL_FAST


def test_stream_insight_routes_deep_mode_to_sonnet():
    stream = _build_canned_stream(
        {"recommendation": "PASS", "confidence": "low", "headline": "x",
         "key_facts": ["x"], "key_risks": ["x"], "suggested_limit_cents": None,
         "tail_warning": False, "disclaimer": "Not investment advice."},
        deltas=["{}"],
    )
    client = _FakeClient(stream)
    list(insight.stream_insight({"market_id": "m1"},
                                 model=insight.MODEL_DEEP, client=client))
    assert client.messages.last_kwargs["model"] == insight.MODEL_DEEP


def test_stream_insight_surfaces_sdk_error_as_error_chunk():
    """SDK exceptions during streaming get converted to an error chunk,
    never re-raised — the endpoint depends on this to keep the SSE
    connection valid."""
    class _Boom:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self):
            raise RuntimeError("network blip")
        def get_final_message(self):
            raise AssertionError("should not be called")

    client = _FakeClient(_Boom())
    chunks = list(insight.stream_insight({"market_id": "m1"}, client=client))
    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert "network blip" in chunks[0].data["error"]


def test_stream_insight_handles_malformed_final_json():
    """If structured-outputs validation somehow returned non-JSON, the
    complete chunk's `insight` is None rather than throwing."""
    stream = _FakeStream(
        [_FakeContentBlockDeltaEvent("garbage")],
        _FakeFinalMessage("not json at all"),
    )
    client = _FakeClient(stream)
    chunks = list(insight.stream_insight({"market_id": "m1"}, client=client))
    complete = chunks[-1].data
    assert complete["insight"] is None


def test_client_factory_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        insight._client()


def test_percentiles_handles_single_member():
    out = insight._percentiles([70.0])
    assert out["p50"] == 70.0
    assert out["p05"] == 70.0
    assert out["p95"] == 70.0


def test_percentiles_handles_empty():
    assert insight._percentiles([]) == {}
