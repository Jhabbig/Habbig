"""Tests for the mid-stream kill-switch re-check and cancellation
hygiene added to ``intelligence.claude_client.stream_intelligence_response``.

Audit finding (MED 3 in ``audits/audit_intelligence.md``):
- The streaming Claude path opened the SDK stream after a single
  kill-switch check, so a switch flipped mid-stream still let the
  stream run to completion.
- The bare ``except Exception`` in the original implementation did not
  catch ``asyncio.CancelledError``, so a FastAPI client disconnect
  during the stream skipped the usage-row write entirely.

These tests:
1. Mock a 10-chunk stream and trip the kill switch when chunk 3
   completes. Assert the consumer receives 3-5 chunks (never the full
   10) and a usage row IS recorded.
2. Mock a stream that raises ``asyncio.CancelledError`` after chunk 2.
   Assert a usage row is recorded before the cancel propagates.

The Anthropic SDK is mocked end-to-end — no network calls.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

# Pin the shared in-memory test DB before importing intelligence/ai.
from tests import _testdb  # noqa: F401

USES_TESTDB = True

# claude_client falls back to a stub message if the SDK / key are
# missing. We DON'T want that path — set both so get_async_client()
# would normally succeed; we then swap it for our fake.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from ai import client as ai_client  # noqa: E402
from intelligence import claude_client as cc  # noqa: E402


def _clear_kill_switch_and_usage_log():
    """Reset the kill-switch + usage table between tests."""
    with _testdb._fake_conn() as conn:
        try:
            conn.execute(
                "UPDATE claude_kill_switch SET active = 0, reason = NULL, "
                "triggered_at = NULL, triggered_by = NULL WHERE id = 1"
            )
        except Exception:
            pass
        try:
            conn.execute("DELETE FROM claude_usage_log")
        except Exception:
            pass


def _usage_row_count() -> int:
    with _testdb._fake_conn() as conn:
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM claude_usage_log"
            ).fetchone()
        except Exception:
            return -1
    return int(row["n"]) if row else 0


# ── Fake SDK helpers ───────────────────────────────────────────────


class _FakeAsyncTextStream:
    """An async iterator that yields the supplied chunks one at a time.

    Optional ``on_yield`` callable fires AFTER each chunk is yielded —
    tests use this to trip the kill switch from "outside" the stream
    at a specific chunk index, or to raise CancelledError mid-stream.
    """

    def __init__(self, chunks, on_yield=None):
        self._chunks = list(chunks)
        self._idx = 0
        self._on_yield = on_yield

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._idx]
        self._idx += 1
        if self._on_yield is not None:
            # Let the callback fire side effects (flip the switch,
            # raise CancelledError, etc.) after we yield this chunk.
            await self._on_yield(self._idx, chunk)
        return chunk


class _FakeStreamContext:
    """Mimics the async context manager returned by sdk.messages.stream(...)."""

    def __init__(self, chunks, on_yield=None, final_usage=(50, 25)):
        self._chunks = chunks
        self._on_yield = on_yield
        self._final_in, self._final_out = final_usage
        self.text_stream = _FakeAsyncTextStream(chunks, on_yield=on_yield)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_final_message(self):
        usage = SimpleNamespace(
            input_tokens=self._final_in,
            output_tokens=self._final_out,
        )
        return SimpleNamespace(usage=usage)


class _FakeSDK:
    def __init__(self, stream_ctx):
        self.messages = SimpleNamespace(stream=lambda **_: stream_ctx)


# ── Tests ──────────────────────────────────────────────────────────


class TestKillSwitchMidStream(unittest.IsolatedAsyncioTestCase):
    """Trip the kill switch partway through a 10-chunk stream and
    confirm the generator stops within the 5-chunk poll window AND a
    usage row is logged for the partial stream.
    """

    def setUp(self):
        _clear_kill_switch_and_usage_log()
        self._orig_get_async = ai_client.get_async_client

    def tearDown(self):
        ai_client.get_async_client = self._orig_get_async
        _clear_kill_switch_and_usage_log()

    async def test_killswitch_trips_at_chunk_3_stream_stops_within_window(self):
        chunks = [f"chunk-{i}" for i in range(1, 11)]

        async def _on_yield(idx, chunk):
            # Flip the switch after the 3rd chunk yields. The re-check
            # runs every 5 chunks, so the generator should raise at
            # chunk 5 (i.e. consumer sees chunks 1..5 then the kill
            # message). We assert <=5 chunks below.
            if idx == 3:
                ai_client.set_kill_switch(
                    active=True, reason="test", triggered_by="pytest",
                )

        fake_ctx = _FakeStreamContext(chunks, on_yield=_on_yield)
        ai_client.get_async_client = lambda: _FakeSDK(fake_ctx)

        received = []
        gen = cc.stream_intelligence_response(
            user={"user_id": 1, "tier": "pro"},
            user_message="hi",
            history=[],
            context_text="ctx",
        )
        async for piece in gen:
            received.append(piece)

        # The consumer should NOT have received all 10 data chunks —
        # the switch trips at chunk 3 and the next mod-5 check (at
        # chunk 5) raises. Allow 3..5 data chunks + 1 kill notice.
        data_chunks = [c for c in received if c.startswith("chunk-")]
        self.assertGreaterEqual(len(data_chunks), 3)
        self.assertLessEqual(len(data_chunks), 5)
        # The generator must yield the kill notice so the user sees why.
        self.assertTrue(any("kill-switch" in c for c in received))

        # A usage row must exist — partial usage logged either via
        # get_final_message() (the response branch) or as a failure row.
        self.assertGreaterEqual(_usage_row_count(), 1)


class TestClientDisconnectMidStream(unittest.IsolatedAsyncioTestCase):
    """If asyncio.CancelledError fires mid-stream (FastAPI's client
    disconnect signal), the function must STILL write a usage row
    before propagating the cancellation.
    """

    def setUp(self):
        _clear_kill_switch_and_usage_log()
        self._orig_get_async = ai_client.get_async_client

    def tearDown(self):
        ai_client.get_async_client = self._orig_get_async
        _clear_kill_switch_and_usage_log()

    async def test_client_disconnect_logs_usage_then_propagates(self):
        chunks = [f"chunk-{i}" for i in range(1, 11)]

        async def _on_yield(idx, chunk):
            # Simulate FastAPI cancelling the task after chunk 2.
            if idx == 2:
                raise asyncio.CancelledError()

        fake_ctx = _FakeStreamContext(chunks, on_yield=_on_yield)
        ai_client.get_async_client = lambda: _FakeSDK(fake_ctx)

        received = []
        gen = cc.stream_intelligence_response(
            user={"user_id": 2, "tier": "pro"},
            user_message="hi",
            history=[],
            context_text="ctx",
        )

        with self.assertRaises(asyncio.CancelledError):
            async for piece in gen:
                received.append(piece)

        # The generator must have logged the failure / usage row BEFORE
        # re-raising the CancelledError. The bare ``except Exception``
        # in the pre-fix code skipped this entirely.
        self.assertGreaterEqual(_usage_row_count(), 1)


if __name__ == "__main__":
    unittest.main()
