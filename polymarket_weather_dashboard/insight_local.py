"""Local LLM client — Ollama-compatible drop-in for the Anthropic SDK.

Talks to any OpenAI-compatible `/v1/chat/completions` endpoint (Ollama
by default at http://localhost:11434, but works with LM Studio, vLLM,
llama.cpp's server, or any other host of the same shape).

Exposes just enough of the Anthropic SDK's surface for `stream_insight`
to use it without any code changes there:

    client.messages.stream(
        model=..., max_tokens=..., system=[...], messages=[...],
        output_config=...
    ) as stream:
        for event in stream:
            if event.type == "content_block_delta" and event.delta.type == "text_delta":
                event.delta.text
        final = stream.get_final_message()
        final.content[0].text
        final.usage.input_tokens
        ...

Tradeoffs vs the real Anthropic client
--------------------------------------
* **Quality**: a 70B local model lags Claude on nuanced reasoning and
  on rare-but-important edge cases. The dashboard's structured JSON
  output is the most fragile surface — local models break schemas more
  often. We address this with retry-on-bad-JSON in the stream wrapper.
* **No prompt caching**: every call processes the full system prompt.
  Without the ~10x cost-cut Claude's cache provides, local "free" can
  still be slow.
* **No structured outputs**: we use `response_format: json_object` mode
  (broadly supported) and rely on the system prompt's documented schema
  to guide the model. Validation happens after parse.
* **Cost = $0** per call, capped by your hardware.

Configuration (env vars)
-----------------------
    LLM_PROVIDER          "anthropic" (default) | "local"
    OLLAMA_BASE_URL       default "http://localhost:11434"
    OLLAMA_MODEL_FAST     default "llama3.1:8b"
    OLLAMA_MODEL_DEEP     default "llama3.3:70b"
    OLLAMA_TIMEOUT        seconds; default 120 (local inference is slow)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Iterator, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 120

# Ollama model defaults — picked to balance "runs on a laptop" and
# "produces sane structured output". Users with a real GPU should swap
# in something larger via env var.
DEFAULT_MODEL_FAST = "llama3.1:8b"
DEFAULT_MODEL_DEEP = "llama3.3:70b"
DEFAULT_MODEL_EXTRA = "qwen2.5:14b"


# ─── Minimal SDK-shaped event/message classes ────────────────────────────────
#
# Just enough surface for stream_insight to walk them without choking.
# Using plain dataclasses (not the SDK's pydantic models) so tests can
# construct them too without an Anthropic dependency.

@dataclass
class _TextDelta:
    text: str
    type: str = "text_delta"


@dataclass
class _ContentBlockDeltaEvent:
    delta: _TextDelta
    type: str = "content_block_delta"


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FinalMessage:
    content: list
    usage: _Usage
    model: str
    stop_reason: str = "end_turn"


# ─── Translation helpers ──────────────────────────────────────────────────────

def _flatten_system_blocks(system_blocks) -> str:
    """Anthropic's `system=[{type:"text", text:"..."}]` → one string.

    Local servers expect a single system message. Cache_control fields
    are silently ignored — local has no equivalent caching layer.
    """
    if not system_blocks:
        return ""
    if isinstance(system_blocks, str):
        return system_blocks
    parts = []
    for block in system_blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("text"):
            parts.append(block["text"])
    return "\n\n".join(parts)


def _to_openai_messages(system_blocks, messages: list) -> list[dict]:
    """Build the OpenAI-style messages array. System block first if
    present; user/assistant content comes through verbatim."""
    out: list[dict] = []
    system_text = _flatten_system_blocks(system_blocks)
    if system_text:
        out.append({"role": "system", "content": system_text})
    for m in messages or []:
        if isinstance(m, dict):
            content = m.get("content", "")
            if isinstance(content, list):
                # Anthropic content blocks → text concat
                content = "\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            out.append({"role": m.get("role", "user"), "content": content})
    return out


def _schema_hint_for_prompt(output_config: Optional[dict]) -> str:
    """When structured outputs are requested but the local server can
    only do `json_object` mode, append a one-line nudge to the user
    message reminding the model what shape we want."""
    if not output_config:
        return ""
    fmt = (output_config or {}).get("format") or {}
    if fmt.get("type") != "json_schema":
        return ""
    return ("\n\nReturn ONLY a single JSON object matching this schema "
            "(the methodology documented above describes the field "
            "meanings):\n```json\n"
            + json.dumps(fmt.get("schema", {}), separators=(",", ":"))
            + "\n```\nNo prose before or after.")


# ─── Stream context manager ──────────────────────────────────────────────────

class _LocalStreamContext:
    """Mimics the SDK's stream context manager. On `__iter__` it
    streams chunks from the local server, yielding our `_ContentBlockDeltaEvent`
    objects. `get_final_message()` returns the assembled response.
    """

    def __init__(self, client: "LocalLLMClient", *, model: str,
                 max_tokens: int, system, messages,
                 output_config=None, **_kw):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.system = system
        self.messages = messages
        self.output_config = output_config
        # Mutable state filled during iteration:
        self._collected_text: list[str] = []
        self._response = None
        self._usage_input = 0
        self._usage_output = 0
        self._stop_reason = "end_turn"
        self._started = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._response is not None:
            try:
                self._response.close()
            except Exception:
                pass
        return False

    def _build_payload(self) -> dict:
        oai_messages = _to_openai_messages(self.system, self.messages)
        if self.output_config:
            hint = _schema_hint_for_prompt(self.output_config)
            if hint and oai_messages and oai_messages[-1]["role"] == "user":
                oai_messages[-1]["content"] = oai_messages[-1]["content"] + hint

        payload: dict = {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": int(self.max_tokens),
            "stream": True,
        }
        # Structured output via the broadly-supported OpenAI flag.
        # Falling back to plain text mode if the user didn't request
        # structured output.
        if self.output_config:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _open(self):
        if self._started:
            return
        self._started = True
        url = f"{self.client.base_url}/v1/chat/completions"
        try:
            self._response = requests.post(
                url, json=self._build_payload(),
                stream=True, timeout=self.client.timeout,
                headers={"Content-Type": "application/json"},
            )
        except requests.RequestException as e:
            raise RuntimeError(f"local LLM request failed: {e}") from e
        if self._response.status_code != 200:
            body = ""
            try:
                body = self._response.text[:500]
            except Exception:
                pass
            raise RuntimeError(
                f"local LLM HTTP {self._response.status_code}: {body}"
            )

    def __iter__(self) -> Iterator:
        self._open()
        for raw in self._response.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            # OpenAI-style SSE: "data: <json>" or "data: [DONE]"
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                event = json.loads(line)
            except ValueError:
                logger.debug("local LLM: skipping unparsable chunk %r", line[:80])
                continue
            # Usage may arrive on the last chunk
            usage = event.get("usage")
            if isinstance(usage, dict):
                self._usage_input = int(usage.get("prompt_tokens") or 0)
                self._usage_output = int(usage.get("completion_tokens") or 0)
            choices = event.get("choices") or []
            for ch in choices:
                delta = ch.get("delta") or {}
                content = delta.get("content")
                if content:
                    self._collected_text.append(content)
                    yield _ContentBlockDeltaEvent(_TextDelta(text=content))
                if ch.get("finish_reason"):
                    fr = ch["finish_reason"]
                    # OpenAI uses "stop" for normal completion;
                    # translate to Anthropic-equivalents.
                    self._stop_reason = {
                        "stop": "end_turn",
                        "length": "max_tokens",
                        "tool_calls": "tool_use",
                        "content_filter": "refusal",
                    }.get(fr, fr)

    def get_final_message(self) -> _FinalMessage:
        text = "".join(self._collected_text)
        return _FinalMessage(
            content=[_TextBlock(text=text)],
            usage=_Usage(input_tokens=self._usage_input,
                         output_tokens=self._usage_output),
            model=self.model,
            stop_reason=self._stop_reason,
        )


class _LocalMessagesAPI:
    """Mirrors the `.messages` namespace on `anthropic.Anthropic`."""
    def __init__(self, client: "LocalLLMClient"):
        self.client = client

    def stream(self, **kwargs) -> _LocalStreamContext:
        return _LocalStreamContext(self.client, **kwargs)


class LocalLLMClient:
    """Drop-in replacement for `anthropic.Anthropic()` that talks to a
    local Ollama-compatible server.

    Usage matches the Anthropic SDK exactly so the rest of the dashboard
    doesn't need to know which provider is active:

        client = LocalLLMClient()
        with client.messages.stream(model="llama3.1:8b", ...) as s:
            ...
    """

    def __init__(self, *,
                 base_url: Optional[str] = None,
                 timeout: Optional[float] = None):
        self.base_url = (base_url or
                          os.environ.get("OLLAMA_BASE_URL") or
                          DEFAULT_BASE_URL).rstrip("/")
        self.timeout = (timeout if timeout is not None
                         else float(os.environ.get("OLLAMA_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)))
        self.messages = _LocalMessagesAPI(self)


# ─── Model name resolution ────────────────────────────────────────────────────
#
# When LLM_PROVIDER=local, the dashboard's MODEL_FAST / MODEL_DEEP
# strings ("claude-haiku-4-5" / "claude-sonnet-4-6") have no meaning on
# the local server — we have to translate them into Ollama model tags.
# These functions are imported by insight.py so the rest of the code
# can keep using the Claude-shaped MODEL_FAST / MODEL_DEEP constants.

def local_model_for(claude_name: str) -> str:
    """Map a Claude model name to its configured local equivalent."""
    fast = os.environ.get("OLLAMA_MODEL_FAST", DEFAULT_MODEL_FAST)
    deep = os.environ.get("OLLAMA_MODEL_DEEP", DEFAULT_MODEL_DEEP)
    extra = os.environ.get("OLLAMA_MODEL_EXTRA", DEFAULT_MODEL_EXTRA)
    mapping = {
        "claude-haiku-4-5":  fast,
        "claude-sonnet-4-6": deep,
        # Opus 4.7 in the ensemble — pick a third local model so the
        # ensemble actually exercises three different models.
        "claude-opus-4-7":   extra,
    }
    return mapping.get(claude_name, fast)
