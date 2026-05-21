"""Generic local-LLM client (OpenAI-compatible Chat Completions API).

Designed to talk to whatever you run locally:
  - Ollama          (default — `ollama serve` → http://localhost:11434/v1)
  - LM Studio       (http://localhost:1234/v1)
  - vLLM            (http://localhost:8000/v1)
  - llama.cpp server (http://localhost:8080/v1)

All of them expose the OpenAI chat-completions schema and respect
`response_format: {"type": "json_object"}` for structured output. We
default the API key to "ollama" because Ollama ignores it; other
backends accept any non-empty string in dev.

Recommended model for filing extraction on a laptop GPU:
    qwen2.5:7b-instruct      (best at structured JSON, ~5GB Q4)
    llama3.1:8b-instruct-q4_K_M
    mistral:7b-instruct-q4

Run `ollama pull qwen2.5:7b-instruct` once, then set:
    LLM_BASE_URL=http://localhost:11434/v1
    LLM_MODEL=qwen2.5:7b-instruct
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import httpx

log = logging.getLogger("llm")

LLM_BASE_URL    = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_MODEL       = os.environ.get("LLM_MODEL", "qwen2.5:7b-instruct")
LLM_API_KEY     = os.environ.get("LLM_API_KEY", "ollama")
LLM_TIMEOUT_S   = float(os.environ.get("LLM_TIMEOUT_S", "180"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0"))
LLM_MAX_TOKENS  = int(os.environ.get("LLM_MAX_TOKENS", "1024"))

# Serialise calls so a slow GPU doesn't get N parallel requests fighting for VRAM.
_sem = asyncio.Semaphore(int(os.environ.get("LLM_CONCURRENCY", "1")))


def is_configured() -> bool:
    return bool(LLM_BASE_URL)


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.M)


def _extract_json_blob(content: str) -> str:
    """Best-effort recovery when a model wraps JSON in fences or prose."""
    m = _JSON_FENCE.search(content)
    if m:
        return m.group(1).strip()
    # First {...} balanced block.
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        return content[start:end + 1]
    return content.strip()


async def chat_json(*, system: str, user: str,
                    model: str | None = None,
                    max_tokens: int | None = None) -> dict | None:
    """Send a chat-completions call expecting a JSON object back.

    Returns the parsed dict, or None on transport error / parse failure.
    Never raises — extraction is best-effort.
    """
    url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    body = {
        "model": model or LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "temperature": LLM_TEMPERATURE,
        "max_tokens":  max_tokens or LLM_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }

    async with _sem:
        try:
            async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as cx:
                r = await cx.post(url, json=body, headers=headers)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.info("llm call failed: %s", e)
            return None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log.info("llm response missing choices: %s", data)
        return None

    if not content:
        return None

    blob = _extract_json_blob(content)
    try:
        return json.loads(blob)
    except json.JSONDecodeError as e:
        log.info("llm json parse failed (%s); content head: %r", e, content[:200])
        return None
