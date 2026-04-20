"""
Fixtures shared across unit + integration suites.

Key guarantees:
  * fresh_db     — every test gets a pristine temp-file SQLite (no cross-talk
                   via the thread-local connection in db.py)
  * mock_anthropic — classifier._get_client is patched to a fake that each
                     test can script (.push_response / .push_raw) so no
                     network, no API key needed. Records calls for assertions.
  * mock_httpx   — respx router yielded so tests can stub Reddit and any
                   other httpx traffic.
  * test_client  — FastAPI TestClient with lifespan-tasks replaced by no-ops
                   so the background loops don't race the test.
  * seeded_db    — fresh_db + seed_test_data.seed(posts, hours).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest
import pytest_asyncio

# Ensure the dashboard package root is on sys.path for `import config`, etc.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Fake Anthropic client ────────────────────────────────────────────────────

@dataclass
class _FakeContentBlock:
    text: str


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class _FakeResponse:
    content: list[_FakeContentBlock]
    usage: _FakeUsage


@dataclass
class _RecordedCall:
    model: str
    system: Optional[str]
    user: str
    max_tokens: int
    temperature: float


class _FakeMessages:
    def __init__(self, parent: "FakeAnthropicClient") -> None:
        self._parent = parent

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float = 0.0,
        system: Optional[str] = None,
        messages: list[dict] | None = None,
    ) -> _FakeResponse:
        user = ""
        if messages:
            first = messages[0]
            content = first.get("content")
            user = content if isinstance(content, str) else json.dumps(content)
        self._parent.calls.append(_RecordedCall(
            model=model, system=system, user=user,
            max_tokens=max_tokens, temperature=temperature,
        ))
        if self._parent.raise_next:
            exc = self._parent.raise_next
            self._parent.raise_next = None
            raise exc
        if not self._parent.responses:
            # Safe default: empty text, zero-cost usage.
            return _FakeResponse(
                content=[_FakeContentBlock(text="")],
                usage=_FakeUsage(input_tokens=0, output_tokens=0),
            )
        return self._parent.responses.pop(0)


class FakeAnthropicClient:
    """Minimal stand-in for anthropic.AsyncAnthropic.

    Tests script responses via push_text / push_json / push_raise.
    """

    def __init__(self) -> None:
        self.responses: list[_FakeResponse] = []
        self.calls: list[_RecordedCall] = []
        self.raise_next: Optional[Exception] = None
        self.messages = _FakeMessages(self)

    def push_text(self, text: str, *, input_tokens: int = 100, output_tokens: int = 50) -> None:
        self.responses.append(_FakeResponse(
            content=[_FakeContentBlock(text=text)],
            usage=_FakeUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        ))

    def push_json(self, payload: Any, *, input_tokens: int = 100, output_tokens: int = 50) -> None:
        self.push_text(json.dumps(payload), input_tokens=input_tokens, output_tokens=output_tokens)

    def push_raise(self, exc: Exception) -> None:
        self.raise_next = exc


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch) -> Iterator[Path]:
    """Temp SQLite + clean thread-local + init_db. Yields the db path."""
    import config
    import db

    path = tmp_path / "annoyance_test.db"
    monkeypatch.setattr(config, "DB_PATH", path)

    # Force the thread-local to rebuild against the new path.
    if hasattr(db._local, "conn") and db._local.conn is not None:
        try:
            db._local.conn.close()
        except Exception:
            pass
        db._local.conn = None

    db.init_db()
    try:
        yield path
    finally:
        if hasattr(db._local, "conn") and db._local.conn is not None:
            try:
                db._local.conn.close()
            except Exception:
                pass
            db._local.conn = None


@pytest.fixture(autouse=True)
def _ensure_schema_on_current_db_path():
    """Safety net: after fresh_db-using tests tear down, monkeypatch reverts
    config.DB_PATH to a pre-existing-test's module-level path. Those tests
    call db.cursor() directly without re-initing. If the thread-local conn
    was cleared mid-session, the next connection is fresh — but the on-disk
    schema better exist. This autouse fixture calls init_db() before every
    test against whatever config.DB_PATH currently points to, so pre-existing
    tests that assume a ready schema still work regardless of test order.
    """
    import db
    try:
        db.init_db()
    except Exception:
        pass
    yield


@pytest.fixture
def mock_anthropic(monkeypatch) -> FakeAnthropicClient:
    """Patch classifier._get_client so no real API traffic ever happens."""
    import classifier
    import config

    # Prevent the "no api key" short-circuit from dodging the patch.
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake = FakeAnthropicClient()
    monkeypatch.setattr(classifier, "_get_client", lambda: fake)
    return fake


@pytest.fixture
def mock_httpx():
    """Respx router. assert_all_called=False so partial stubs don't fail."""
    import respx
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def test_client(fresh_db, monkeypatch):
    """FastAPI TestClient with lifespan loops replaced by no-ops."""
    async def _noop() -> None:
        return

    # Patch the loop functions BEFORE import of app triggers lifespan wiring.
    import server
    monkeypatch.setattr(server, "reddit_loop", _noop)
    monkeypatch.setattr(server, "classifier_loop", _noop)
    monkeypatch.setattr(server, "aggregator_loop", _noop)
    monkeypatch.setattr(server, "spike_detector_loop", _noop)
    monkeypatch.setattr(server, "retention_loop", _noop)

    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        yield client


@pytest.fixture
def seeded_db(fresh_db):
    """fresh_db + synthetic posts/classifications via seed_test_data."""
    import seed_test_data
    seed_test_data.seed(num_posts=200, hours=48)
    return fresh_db


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear the rate limiter between tests. Source backoff state is managed
    by each source test file's own setUp/tearDown so we don't touch it here
    — clearing it in an autouse can race with unittest.TestCase's own reset
    ordering and break pre-existing tests."""
    try:
        import rate_limiter
        rate_limiter.reset_for_tests()
    except Exception:
        pass
    yield


# ── Gateway SSO helpers ──────────────────────────────────────────────────────

GATEWAY_SECRET = "test-gateway-sso-secret"


@pytest.fixture
def paywall_env(monkeypatch):
    """Set GATEWAY_SSO_SECRET so auth.get_session_user will validate."""
    monkeypatch.setenv("GATEWAY_SSO_SECRET", GATEWAY_SECRET)
    return GATEWAY_SECRET


def pro_headers(user_id: int = 42, email: str = "pro@example.test") -> dict[str, str]:
    return {
        "X-Gateway-Secret": GATEWAY_SECRET,
        "X-Gateway-User-ID": str(user_id),
        "X-Gateway-User-Email": email,
        "X-Gateway-User-Tier": "pro",
    }


def admin_headers(user_id: int = 1, email: str = "admin@narve.ai") -> dict[str, str]:
    return {
        "X-Gateway-Secret": GATEWAY_SECRET,
        "X-Gateway-User-ID": str(user_id),
        "X-Gateway-User-Email": email,
        "X-Gateway-User-Tier": "super_admin",
    }


def free_headers(user_id: int = 99, email: str = "free@example.test") -> dict[str, str]:
    return {
        "X-Gateway-Secret": GATEWAY_SECRET,
        "X-Gateway-User-ID": str(user_id),
        "X-Gateway-User-Email": email,
        "X-Gateway-User-Tier": "free",
    }


@pytest.fixture
def as_localhost(monkeypatch):
    """Make auth._client_host return 127.0.0.1 so /admin/* routes accept."""
    import auth
    monkeypatch.setattr(auth, "_client_host", lambda request: "127.0.0.1")
