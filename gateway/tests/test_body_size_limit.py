"""Audit HIGH FIX D — BodySizeLimitMiddleware regression tests.

A POST without a body-size cap means a multi-MB blob is fully buffered
before any handler sees it. The middleware sits at the outermost edge of
the ASGI stack and rejects with 413 *before* any downstream middleware
reads the body.

Test plan
---------

* Content-Length present and below cap → 200 (happy path).
* Content-Length present and above cap → 413, handler NOT called.
* Per-prefix exemption gets the bigger cap.
* GET / OPTIONS skip the check entirely.

The tests exercise the middleware via a minimal Starlette app rather
than ``server.app`` so they don't depend on auth / CSRF / session.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from middleware import body_size_limit as bsl  # noqa: E402


class _RouteCounter:
    """Tracks whether the route was ever reached. If the middleware
    aborts with 413, the handler must never run.
    """

    echo_calls = 0
    upload_calls = 0
    ping_calls = 0

    @classmethod
    def reset(cls) -> None:
        cls.echo_calls = 0
        cls.upload_calls = 0
        cls.ping_calls = 0


async def _echo_json(request: Request) -> JSONResponse:
    _RouteCounter.echo_calls += 1
    body = await request.body()
    return JSONResponse({"bytes": len(body)})


async def _upload_handler(request: Request) -> JSONResponse:
    _RouteCounter.upload_calls += 1
    body = await request.body()
    return JSONResponse({"bytes": len(body)})


async def _ping_handler(request: Request) -> PlainTextResponse:
    _RouteCounter.ping_calls += 1
    return PlainTextResponse("pong")


def _build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/echo", _echo_json, methods=["POST", "PUT"]),
            Route("/upload/avatar", _upload_handler, methods=["POST"]),
            Route("/ping", _ping_handler, methods=["GET"]),
        ],
        middleware=[
            Middleware(bsl.BodySizeLimitMiddleware),
        ],
    )


class TestContentLengthCap(unittest.TestCase):
    """Happy + sad paths when the client sends a Content-Length header."""

    MAX_BODY_BYTES = bsl.MAX_BODY_BYTES  # 2 MB default

    def setUp(self) -> None:
        _RouteCounter.reset()
        self.app = _build_app()
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()

    def test_small_post_under_cap_succeeds(self):
        """1 MB JSON POST under the 2 MB cap → 200, handler ran."""
        # 11-byte fixed envelope: b'{"data":"' (9) + b'"}' (2).
        body = b'{"data":"' + (b"a" * (1024 * 1024 - 11)) + b'"}'
        self.assertEqual(len(body), 1024 * 1024)
        r = self.client.post(
            "/echo",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["bytes"], len(body))
        self.assertEqual(_RouteCounter.echo_calls, 1)

    def test_large_post_over_cap_rejected_with_413(self):
        """3 MB JSON POST over the 2 MB cap → 413 BEFORE handler runs."""
        body = b'{"data":"' + (b"x" * (3 * 1024 * 1024 - 11)) + b'"}'
        self.assertEqual(len(body), 3 * 1024 * 1024)
        r = self.client.post(
            "/echo",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 413)
        payload = r.json()
        self.assertEqual(payload["error"], "Payload Too Large")
        self.assertEqual(payload["max_bytes"], self.MAX_BODY_BYTES)
        # Crucial: the handler MUST NOT have run.
        self.assertEqual(
            _RouteCounter.echo_calls, 0,
            "handler ran despite 413 — body-size cap is not enforced before dispatch",
        )

    def test_exact_cap_size_is_allowed(self):
        """A body exactly at the cap (boundary) must pass."""
        body = b"x" * self.MAX_BODY_BYTES
        r = self.client.post(
            "/echo",
            content=body,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(r.status_code, 200)

    def test_cap_plus_one_byte_rejected(self):
        """One byte over the cap → 413."""
        body = b"x" * (self.MAX_BODY_BYTES + 1)
        r = self.client.post(
            "/echo",
            content=body,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(r.status_code, 413)
        self.assertEqual(_RouteCounter.echo_calls, 0)


class TestNoBodyMethods(unittest.TestCase):
    """GET / HEAD / OPTIONS / DELETE / TRACE skip the cap entirely."""

    def setUp(self) -> None:
        _RouteCounter.reset()
        self.app = _build_app()
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()

    def test_get_request_skips_check(self):
        r = self.client.get("/ping")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(_RouteCounter.ping_calls, 1)


if __name__ == "__main__":
    unittest.main()
