"""
Tests for logging_config — structured JSON output, scrubbing, request
context, ring buffer, BetterStack optionality.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging_config as lc  # noqa: E402


class _CapturingHandler(logging.Handler):
    """Simple handler that stores the formatted output for inspection."""

    def __init__(self, formatter):
        super().__init__()
        self.setFormatter(formatter)
        self.records: list[str] = []

    def emit(self, record):  # pragma: no cover — standard path
        try:
            self.records.append(self.format(record))
        except Exception:
            self.handleError(record)


class TestStructuredFormatter(unittest.TestCase):
    def setUp(self):
        self.formatter = lc.StructuredFormatter()
        self.logger = logging.getLogger("tests.structured")
        self.logger.handlers.clear()
        self.logger.setLevel(logging.DEBUG)
        self.handler = _CapturingHandler(self.formatter)
        self.logger.addHandler(self.handler)
        lc.clear_request_context()

    def tearDown(self):
        lc.clear_request_context()

    def _last_json(self) -> dict:
        self.assertTrue(self.handler.records, "no records captured")
        return json.loads(self.handler.records[-1])

    def test_emits_valid_json(self):
        self.logger.info("hello world")
        rec = self._last_json()
        self.assertEqual(rec["message"], "hello world")
        self.assertEqual(rec["level"], "INFO")
        self.assertEqual(rec["logger"], "tests.structured")
        self.assertIn("timestamp", rec)
        self.assertIn("service", rec)
        self.assertIn("environment", rec)

    def test_extra_fields_included(self):
        self.logger.info("pipeline", extra={"count": 47, "duration_ms": 120})
        rec = self._last_json()
        self.assertEqual(rec["count"], 47)
        self.assertEqual(rec["duration_ms"], 120)

    def test_password_scrubbed(self):
        self.logger.info("auth", extra={"password": "s3cret!"})
        rec = self._last_json()
        self.assertEqual(rec["password"], "[REDACTED]")

    def test_token_scrubbed(self):
        self.logger.info("auth", extra={"auth_token": "abc", "api_key": "xyz"})
        rec = self._last_json()
        self.assertEqual(rec["auth_token"], "[REDACTED]")
        self.assertEqual(rec["api_key"], "[REDACTED]")

    def test_allowlist_not_scrubbed(self):
        """request_id, user_id, token_id are NOT secrets — keep them."""
        self.logger.info("req", extra={
            "request_id": "abc",
            "user_id": 42,
            "token_id": 7,
        })
        rec = self._last_json()
        self.assertEqual(rec["request_id"], "abc")
        self.assertEqual(rec["user_id"], 42)
        self.assertEqual(rec["token_id"], 7)

    def test_case_insensitive_scrubbing(self):
        self.logger.info("weird", extra={"Authorization": "Bearer xyz"})
        rec = self._last_json()
        self.assertEqual(rec["Authorization"], "[REDACTED]")

    def test_request_context_added(self):
        lc.set_request_context("req-123", user_id=99)
        self.logger.info("inside request")
        rec = self._last_json()
        self.assertEqual(rec["request_id"], "req-123")
        self.assertEqual(rec["user_id"], 99)

    def test_context_cleared_between_requests(self):
        lc.set_request_context("req-1", user_id=1)
        self.logger.info("req1")
        lc.clear_request_context()
        self.logger.info("req2")
        recs = [json.loads(r) for r in self.handler.records]
        self.assertEqual(recs[0].get("request_id"), "req-1")
        self.assertNotIn("request_id", recs[1])
        self.assertNotIn("user_id", recs[1])

    def test_exception_info_captured(self):
        try:
            raise ValueError("boom")
        except ValueError:
            self.logger.exception("something broke")
        rec = self._last_json()
        self.assertIn("exception", rec)
        self.assertIn("ValueError", rec["exception"])
        self.assertIn("boom", rec["exception"])

    def test_non_serialisable_extra_is_stringified(self):
        class Weird:
            def __repr__(self):
                return "<weird>"
        self.logger.info("weird obj", extra={"obj": Weird()})
        # Should not raise; the record should parse.
        rec = self._last_json()
        self.assertIn("obj", rec)


class TestSecurityLogFilter(unittest.TestCase):
    def test_only_security_passes(self):
        f = lc.SecurityLogFilter()
        rec = logging.LogRecord("security", logging.WARNING, "", 0, "msg", (), None)
        self.assertTrue(f.filter(rec))
        rec2 = logging.LogRecord("security.csrf", logging.WARNING, "", 0, "msg", (), None)
        self.assertTrue(f.filter(rec2))
        rec3 = logging.LogRecord("gateway", logging.WARNING, "", 0, "msg", (), None)
        self.assertFalse(f.filter(rec3))


class TestRingBuffer(unittest.TestCase):
    def setUp(self):
        self.buf = lc.InMemoryRingBuffer(capacity=5)
        self.buf.setFormatter(lc.StructuredFormatter())

    def _emit(self, level_name: str, message: str, service: str = "app", logger: str = "t"):
        record = logging.LogRecord(logger, getattr(logging, level_name), "", 0, message, (), None)
        record.levelname = level_name
        # Force service field via a temporary env override would be overkill —
        # the ring buffer stores whatever the formatter emits.
        self.buf.emit(record)

    def test_keeps_last_n(self):
        for i in range(10):
            self._emit("INFO", f"msg{i}")
        self.assertEqual(len(self.buf), 5)
        snap = self.buf.snapshot(limit=5)
        messages = [r["message"] for r in snap]
        self.assertEqual(messages, ["msg5", "msg6", "msg7", "msg8", "msg9"])

    def test_level_filter(self):
        self._emit("INFO", "info msg")
        self._emit("WARNING", "warn msg")
        self._emit("ERROR", "err msg")
        snap = self.buf.snapshot(level="WARNING", limit=10)
        levels = {r["level"] for r in snap}
        self.assertIn("WARNING", levels)
        self.assertIn("ERROR", levels)
        self.assertNotIn("INFO", levels)

    def test_contains_filter(self):
        self._emit("INFO", "hello world")
        self._emit("INFO", "goodbye cruel world")
        self._emit("INFO", "unrelated")
        snap = self.buf.snapshot(contains="world", limit=10)
        self.assertEqual(len(snap), 2)

    def test_clear(self):
        self._emit("INFO", "x")
        self.assertEqual(len(self.buf), 1)
        self.buf.clear()
        self.assertEqual(len(self.buf), 0)


class TestConfigureLogging(unittest.TestCase):
    def tearDown(self):
        lc.reset_for_tests()

    def test_idempotent(self):
        """Calling configure_logging twice must not duplicate handlers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lc.reset_for_tests()
            lc.configure_logging(base_dir=Path(tmpdir), force=True)
            root = logging.getLogger()
            first_count = len(root.handlers)
            lc.configure_logging(base_dir=Path(tmpdir))  # no force — should no-op
            self.assertEqual(len(root.handlers), first_count)

    def test_force_rebuilds(self):
        """After force=True, file/console handlers are rebuilt but the
        ring-buffer singleton is intentionally preserved across reconfigures
        so the admin panel's in-memory history survives."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lc.reset_for_tests()
            lc.configure_logging(base_dir=Path(tmpdir), force=True)
            first_handlers = list(logging.getLogger().handlers)
            lc.configure_logging(base_dir=Path(tmpdir), force=True)
            second_handlers = list(logging.getLogger().handlers)
            self.assertEqual(len(first_handlers), len(second_handlers))
            # The ring buffer MUST be the same object (persistent across reloads).
            self.assertIn(lc.ring_buffer, first_handlers)
            self.assertIn(lc.ring_buffer, second_handlers)
            # Every non-ring-buffer handler must be a fresh instance.
            fresh_non_ring = [h for h in second_handlers if h is not lc.ring_buffer]
            stale_non_ring = [h for h in first_handlers if h is not lc.ring_buffer]
            for h in stale_non_ring:
                self.assertNotIn(h, fresh_non_ring)

    def test_missing_logtail_token_does_not_crash(self):
        """If LOGTAIL_TOKEN_APP is unset, configure_logging must still succeed."""
        orig = os.environ.pop("LOGTAIL_TOKEN_APP", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                lc.reset_for_tests()
                lc.configure_logging(base_dir=Path(tmpdir), force=True)
                self.assertFalse(lc.is_logtail_configured())
        finally:
            if orig is not None:
                os.environ["LOGTAIL_TOKEN_APP"] = orig

    def test_creates_logs_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lc.reset_for_tests()
            lc.configure_logging(base_dir=Path(tmpdir), force=True)
            self.assertTrue((Path(tmpdir) / "logs").exists())


class TestNoPrintInProductionCode(unittest.TestCase):
    """Grep-check: no bare print() in production paths."""

    EXCLUDED_PREFIXES = (
        "tests/",
        "scraper/tests/",
        "scraper/setup_twitter_session.py",
        "scraper/setup_truthsocial_session.py",
        "migrations/",
        ".pytest_cache/",
        # Operator-facing CLI tools — print() is intentional UX.
        "scripts/",
    )
    EXCLUDED_EXACT = {
        "backend/markets/encryption.py",          # docstring only
        "setup_cloudflare.sh",
        # Both scraper files contain a setup_session() method that is only
        # ever invoked from the interactive setup_*_session.py scripts. The
        # prints are intentional UX for a human running the setup step.
        "scraper/scrapers/twitter.py",
        "scraper/scrapers/truthsocial.py",
        # Forensics watermark extractor is a CLI tool — prints decoded
        # user_id + confidence to stdout when run from the command line.
        "forensics/extract_watermark.py",
        # i18n auto-translate is a manual CLI tool (python3 -m
        # i18n.auto_translate). Prints status + stderr warnings as
        # operator feedback; not part of the request-path surface.
        "i18n/auto_translate.py",
    }

    def test_no_print_statements(self):
        repo_root = Path(__file__).resolve().parent.parent
        try:
            result = subprocess.run(
                ["grep", "-rn", "--include=*.py", r"^[^#]*\bprint(", str(repo_root)],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            self.skipTest("grep unavailable")
        offending = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            abs_path = parts[0]
            rel_path = str(Path(abs_path).resolve().relative_to(repo_root))
            if rel_path in self.EXCLUDED_EXACT:
                continue
            if any(rel_path.startswith(p) for p in self.EXCLUDED_PREFIXES):
                continue
            # Allow prints inside comments and docstrings — this is a rough
            # lint, not a parser. Skip any line that looks like "# print(".
            stripped = parts[2].lstrip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            offending.append(f"{rel_path}:{parts[1]}")
        self.assertFalse(offending,
                         f"Bare print() found in production code:\n  " + "\n  ".join(offending))


if __name__ == "__main__":
    unittest.main()
