"""Deterministic Claude mock.

Two flavours:

  * ``MockAnthropicClient`` / ``MockAsyncAnthropic`` — duck-typed so the
    real anthropic SDK import isn't needed. Matches the subset of the
    API the gateway actually uses: ``.messages.create(...)`` and
    ``.messages.stream(...)``.

  * ``mock_anthropic`` pytest fixture — monkey-patches
    ``ai.client.get_async_client`` to return the async mock, and
    registers the outgoing-prompt → canned-response mapping so tests
    can assert on exact payloads.

Usage::

    def test_extractor(mock_anthropic):
        mock_anthropic.register("Post by @alice", '[{"claim":"x"}]')
        result = run_extractor(...)
        assert mock_anthropic.calls[0]["prompt"].startswith("Post by @alice")
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest


DEFAULT_RESPONSE = '{"predictions": []}'


class _MockMessagesCreate:
    """Callable stand-in for ``client.messages.create``."""

    def __init__(self, parent: "MockAnthropicClient"):
        self._parent = parent

    def __call__(self, *, model: str, max_tokens: int,
                 system: str, messages: list[dict], **kw) -> Any:
        return self._parent._create(model=model, max_tokens=max_tokens,
                                    system=system, messages=messages)


class _AsyncMockMessagesCreate:
    def __init__(self, parent: "MockAnthropicClient"):
        self._parent = parent

    async def __call__(self, *, model: str, max_tokens: int,
                       system: str, messages: list[dict], **kw) -> Any:
        return self._parent._create(model=model, max_tokens=max_tokens,
                                    system=system, messages=messages)


class MockAnthropicClient:
    """Sync variant — used by code paths that instantiate
    ``anthropic.Anthropic(...)`` (none remain in production, but several
    legacy tests still stub this shape)."""

    def __init__(self, responses: Optional[dict[str, str]] = None):
        # Keys are matched as substrings of the first user-message content.
        # A special key ``"default"`` is the fallback.
        self.responses: dict[str, str] = dict(responses or {})
        self.calls: list[dict] = []

    @property
    def messages(self):
        return SimpleNamespace(create=_MockMessagesCreate(self))

    # ── Internal ─────────────────────────────────────────────────
    def _create(self, *, model: str, max_tokens: int,
                system: str, messages: list[dict]) -> Any:
        first_user = messages[0]["content"] if messages else ""
        self.calls.append({
            "model": model, "max_tokens": max_tokens,
            "system": system, "prompt": first_user,
        })
        text = self.responses.get("default", DEFAULT_RESPONSE)
        for needle, canned in self.responses.items():
            if needle == "default":
                continue
            if needle in first_user:
                text = canned
                break
        return _MockResponse(text)

    # ── Public API for tests ─────────────────────────────────────
    def register(self, needle: str, response: str) -> None:
        """Pin a canned response for any prompt that contains ``needle``."""
        self.responses[needle] = response

    def register_default(self, response: str) -> None:
        self.responses["default"] = response


class MockAsyncAnthropic(MockAnthropicClient):
    """Async variant — what ``ai.client.get_async_client`` returns."""

    @property
    def messages(self):
        return SimpleNamespace(create=_AsyncMockMessagesCreate(self))


class _MockResponse:
    """Duck-typed ``anthropic.types.Message`` with usage + content."""

    def __init__(self, text: str, in_tokens: int = 100, out_tokens: int = 50):
        self.content = [SimpleNamespace(text=text, type="text")]
        self.usage = SimpleNamespace(
            input_tokens=in_tokens, output_tokens=out_tokens,
        )
        self.id = "msg_test"
        self.model = "claude-haiku-4-5-20251001"
        self.stop_reason = "end_turn"


# ── Pytest fixture ───────────────────────────────────────────────────────


@pytest.fixture
def mock_anthropic(monkeypatch):
    """Replace the gateway's Claude client with the async mock.

    Yields the mock instance so tests can register canned replies or
    assert on ``mock.calls``. Also pins the kill-switch off so the
    check-and-short-circuit path in ``ai.client.call_claude`` doesn't
    stall the test on a prior test's flipped flag.
    """
    mock = MockAsyncAnthropic()
    monkeypatch.setattr("ai.client.get_async_client", lambda: mock)
    # Silence the kill switch check — tests shouldn't inherit state.
    try:
        from ai import client as _ai_client
        monkeypatch.setattr(_ai_client, "is_kill_switch_active", lambda: False)
    except Exception:
        pass
    yield mock
