"""Tests for the Ollama-compatible local LLM adapter.

Every test mocks `requests.post` — nothing actually hits a local server.
The shape we verify is what the existing `stream_insight` reads:

  * Iteration yields events with `.type == "content_block_delta"` and
    `event.delta.text` populated.
  * `get_final_message()` returns an object with `.content[0].text`,
    `.usage.input_tokens`, `.usage.output_tokens`, etc.

And we check the request body translation:

  * Anthropic-shaped `system=[{type:"text", text:"..."}]` flattens to
    one OpenAI system message.
  * `output_config` adds `response_format: json_object` and a schema
    hint at the end of the user message.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import insight_local as il


# ─── _flatten_system_blocks ──────────────────────────────────────────────────

def test_flatten_anthropic_system_blocks_to_string():
    blocks = [
        {"type": "text", "text": "part one", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "part two"},
    ]
    out = il._flatten_system_blocks(blocks)
    assert "part one" in out
    assert "part two" in out
    # cache_control silently dropped — local has no equivalent
    assert "cache_control" not in out


def test_flatten_accepts_plain_string():
    assert il._flatten_system_blocks("just a string") == "just a string"


def test_flatten_empty_returns_empty_string():
    assert il._flatten_system_blocks(None) == ""
    assert il._flatten_system_blocks([]) == ""


# ─── _to_openai_messages ──────────────────────────────────────────────────────

def test_messages_translation_puts_system_first():
    msgs = il._to_openai_messages(
        [{"type": "text", "text": "you are a helper"}],
        [{"role": "user", "content": "hi"}],
    )
    assert msgs[0]["role"] == "system"
    assert "helper" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hi"}


def test_messages_translation_no_system_when_blocks_empty():
    msgs = il._to_openai_messages([], [{"role": "user", "content": "hi"}])
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_messages_translation_flattens_anthropic_content_blocks():
    """Anthropic allows `content: [{type:"text", text:"..."}, ...]` in
    user messages; OpenAI wants a plain string."""
    msgs = il._to_openai_messages(
        [],
        [{"role": "user", "content": [
            {"type": "text", "text": "line one"},
            {"type": "text", "text": "line two"},
        ]}],
    )
    assert "line one" in msgs[0]["content"]
    assert "line two" in msgs[0]["content"]


# ─── _schema_hint_for_prompt ─────────────────────────────────────────────────

def test_schema_hint_renders_when_json_schema_requested():
    cfg = {"format": {"type": "json_schema", "schema": {"required": ["x"]}}}
    hint = il._schema_hint_for_prompt(cfg)
    assert "JSON object" in hint
    assert '"required"' in hint
    assert '"x"' in hint


def test_schema_hint_empty_when_no_output_config():
    assert il._schema_hint_for_prompt(None) == ""
    assert il._schema_hint_for_prompt({}) == ""


def test_schema_hint_empty_for_non_json_schema_format():
    cfg = {"format": {"type": "text"}}
    assert il._schema_hint_for_prompt(cfg) == ""


# ─── local_model_for ──────────────────────────────────────────────────────────

def test_local_model_for_uses_env_defaults(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL_FAST", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL_DEEP", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL_EXTRA", raising=False)
    assert il.local_model_for("claude-haiku-4-5") == il.DEFAULT_MODEL_FAST
    assert il.local_model_for("claude-sonnet-4-6") == il.DEFAULT_MODEL_DEEP
    assert il.local_model_for("claude-opus-4-7") == il.DEFAULT_MODEL_EXTRA


def test_local_model_for_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL_FAST", "mistral:7b")
    monkeypatch.setenv("OLLAMA_MODEL_DEEP", "qwen2.5:72b")
    assert il.local_model_for("claude-haiku-4-5") == "mistral:7b"
    assert il.local_model_for("claude-sonnet-4-6") == "qwen2.5:72b"


def test_local_model_for_unknown_falls_back_to_fast(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL_FAST", "fallback-model")
    assert il.local_model_for("not-a-claude-name") == "fallback-model"


# ─── Stream context (mocked HTTP) ─────────────────────────────────────────────

def _sse_chunk(text=None, finish=None, usage=None):
    """Build one OpenAI-style SSE data line."""
    delta = {}
    if text is not None:
        delta["content"] = text
    body = {"choices": [{"delta": delta}]}
    if finish:
        body["choices"][0]["finish_reason"] = finish
    if usage:
        body["usage"] = usage
    return f"data: {json.dumps(body)}"


class _FakeResponse:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code
        self.text = ""

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line

    def close(self):
        pass


def test_stream_yields_token_deltas_in_order(monkeypatch):
    """Three SSE chunks → three content_block_delta events, then a
    final message stitched from the concatenated deltas."""
    lines = [
        _sse_chunk(text="hello "),
        _sse_chunk(text="from "),
        _sse_chunk(text="ollama"),
        _sse_chunk(finish="stop", usage={"prompt_tokens": 100, "completion_tokens": 3}),
        "data: [DONE]",
    ]
    monkeypatch.setattr(il.requests, "post",
                        MagicMock(return_value=_FakeResponse(lines)))
    client = il.LocalLLMClient()
    with client.messages.stream(
        model="llama3.1:8b", max_tokens=512,
        system=[{"type": "text", "text": "sys"}],
        messages=[{"role": "user", "content": "hi"}],
    ) as stream:
        deltas = [e.delta.text for e in stream]
        final = stream.get_final_message()
    assert deltas == ["hello ", "from ", "ollama"]
    assert final.content[0].text == "hello from ollama"
    assert final.usage.input_tokens == 100
    assert final.usage.output_tokens == 3
    assert final.stop_reason == "end_turn"


def test_stream_translates_finish_reasons(monkeypatch):
    lines = [_sse_chunk(text="x"),
             _sse_chunk(finish="length"),
             "data: [DONE]"]
    monkeypatch.setattr(il.requests, "post",
                        MagicMock(return_value=_FakeResponse(lines)))
    client = il.LocalLLMClient()
    with client.messages.stream(
        model="m", max_tokens=512, system="", messages=[{"role":"user","content":"x"}],
    ) as stream:
        list(stream)
        final = stream.get_final_message()
    # OpenAI "length" → Anthropic "max_tokens"
    assert final.stop_reason == "max_tokens"


def test_stream_skips_unparsable_chunks(monkeypatch):
    """Malformed SSE shouldn't crash the iterator."""
    lines = [
        "data: not json",
        _sse_chunk(text="ok"),
        "garbage line no prefix",
        "data: [DONE]",
    ]
    monkeypatch.setattr(il.requests, "post",
                        MagicMock(return_value=_FakeResponse(lines)))
    client = il.LocalLLMClient()
    with client.messages.stream(
        model="m", max_tokens=512, system="", messages=[{"role":"user","content":"x"}],
    ) as stream:
        deltas = [e.delta.text for e in stream]
    assert deltas == ["ok"]


def test_stream_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(il.requests, "post",
                        MagicMock(return_value=_FakeResponse([], status_code=500)))
    client = il.LocalLLMClient()
    with pytest.raises(RuntimeError, match="HTTP 500"):
        with client.messages.stream(
            model="m", max_tokens=512, system="",
            messages=[{"role":"user","content":"x"}],
        ) as stream:
            list(stream)


def test_stream_raises_on_network_error(monkeypatch):
    def boom(*a, **kw):
        raise il.requests.ConnectionError("dns dead")
    monkeypatch.setattr(il.requests, "post", boom)
    client = il.LocalLLMClient()
    with pytest.raises(RuntimeError, match="request failed"):
        with client.messages.stream(
            model="m", max_tokens=512, system="",
            messages=[{"role":"user","content":"x"}],
        ) as stream:
            list(stream)


def test_stream_payload_includes_schema_hint_when_output_config_set(monkeypatch):
    """When the caller asks for structured output, the user message
    gets a schema hint appended and response_format is json_object."""
    captured = {}
    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["body"] = kwargs.get("json", {})
        return _FakeResponse([_sse_chunk(text="{}"),
                              _sse_chunk(finish="stop"),
                              "data: [DONE]"])
    monkeypatch.setattr(il.requests, "post", fake_post)

    client = il.LocalLLMClient(base_url="http://localhost:9999")
    schema = {"type": "object", "properties": {"recommendation": {"type": "string"}}}
    with client.messages.stream(
        model="llama3.1:8b", max_tokens=2048,
        system=[{"type": "text", "text": "system prompt"}],
        messages=[{"role": "user", "content": "analyze this"}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    ) as stream:
        list(stream)

    assert captured["url"] == "http://localhost:9999/v1/chat/completions"
    body = captured["body"]
    assert body["model"] == "llama3.1:8b"
    assert body["stream"] is True
    assert body["response_format"] == {"type": "json_object"}
    # System message first, then user with schema hint appended
    assert body["messages"][0]["role"] == "system"
    assert "system prompt" in body["messages"][0]["content"]
    user_content = body["messages"][1]["content"]
    assert "analyze this" in user_content
    assert "recommendation" in user_content  # schema hint includes the schema text


def test_stream_payload_omits_response_format_without_output_config(monkeypatch):
    captured = {}
    def fake_post(url, **kwargs):
        captured["body"] = kwargs.get("json", {})
        return _FakeResponse(["data: [DONE]"])
    monkeypatch.setattr(il.requests, "post", fake_post)
    client = il.LocalLLMClient()
    with client.messages.stream(
        model="m", max_tokens=128, system="",
        messages=[{"role": "user", "content": "hi"}],
    ) as stream:
        list(stream)
    assert "response_format" not in captured["body"]


# ─── Provider selection in insight.py ────────────────────────────────────────

def test_active_provider_default_anthropic(monkeypatch):
    import insight
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert insight.active_provider() == "anthropic"


def test_active_provider_local_via_env(monkeypatch):
    import insight
    monkeypatch.setenv("LLM_PROVIDER", "local")
    assert insight.active_provider() == "local"
    monkeypatch.setenv("LLM_PROVIDER", "ollama")  # alias
    assert insight.active_provider() == "local"


def test_resolve_model_name_passthrough_for_anthropic(monkeypatch):
    import insight
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert insight.resolve_model_name("claude-haiku-4-5") == "claude-haiku-4-5"
    assert insight.resolve_model_name("anything") == "anything"


def test_resolve_model_name_translates_for_local(monkeypatch):
    import insight
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("OLLAMA_MODEL_FAST", "phi3:mini")
    assert insight.resolve_model_name("claude-haiku-4-5") == "phi3:mini"


def test_client_factory_uses_local_when_provider_set(monkeypatch):
    import insight
    monkeypatch.setenv("LLM_PROVIDER", "local")
    # No ANTHROPIC_API_KEY needed — local has no auth
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = insight._client()
    assert isinstance(client, il.LocalLLMClient)


def test_client_factory_local_no_api_key_required(monkeypatch):
    """The 'no key' guardrail only applies when provider=anthropic."""
    import insight
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Should not raise
    insight._client()
