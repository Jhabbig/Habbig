"""Structured logging + Sentry scrub tests.

We exercise the JSONFormatter directly (so we don't depend on installing
sentry-sdk in CI) and confirm that sensitive values never land in the
emitted log line.
"""

from __future__ import annotations

import json
import logging

from observability import JSONFormatter, scrub_sensitive_data


def _emit(record: logging.LogRecord) -> dict:
    formatter = JSONFormatter(service="annoyance", environment="test")
    line = formatter.format(record)
    return json.loads(line)


def _make_record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="annoyance.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_secret_scrubbed_from_logs():
    record = _make_record(authorization="Bearer xyz", gateway_secret="super-secret-value")
    data = _emit(record)
    for key, value in data.items():
        assert "super-secret-value" not in str(value), f"{key} leaked secret"
    assert data["authorization"] == "[REDACTED]"
    assert data["gateway_secret"] == "[REDACTED]"


def test_api_key_scrubbed_from_logs():
    record = _make_record(anthropic_api_key="sk-ant-abc", api_key="sk-ant-abc")
    data = _emit(record)
    assert data["anthropic_api_key"] == "[REDACTED]"
    assert data["api_key"] == "[REDACTED]"
    assert "sk-ant-abc" not in json.dumps(data)


def test_content_redacted_on_warning_level():
    """Post content is only redacted for WARNING+ to stop crash payloads
    from leaking user speech into Sentry. INFO logs may still include it
    (e.g. for local debugging when content is explicitly passed)."""
    warn = logging.LogRecord(
        name="annoyance.test",
        level=logging.WARNING,
        pathname=__file__, lineno=1, msg="post processed",
        args=(), exc_info=None,
    )
    setattr(warn, "content", "my private rant")
    data = _emit(warn)
    assert data["content"] == "[REDACTED]"


def test_content_preserved_on_info_level():
    record = _make_record(content="benign debug text")
    data = _emit(record)
    # At INFO, content survives the formatter (classifier_loop logs post
    # excerpts for operators to sanity-check).
    assert data["content"] == "benign debug text"


def test_sentry_before_send_scrubs_headers():
    event = {
        "level": "error",
        "request": {
            "headers": {
                "Authorization": "Bearer xyz",
                "X-Gateway-Secret": "my-secret",
                "X-Anthropic-Api-Key": "sk-ant-abc",
                "User-Agent": "Mozilla/5.0",
            },
            "cookies": {"session": "abc"},
            "data": {"post_id": 42, "password": "hunter2"},
        },
        "extra": {"post_content": "a user's post"},
    }
    scrubbed = scrub_sensitive_data(event)
    hdrs = scrubbed["request"]["headers"]
    assert hdrs["Authorization"] == "[Filtered]"
    assert hdrs["X-Gateway-Secret"] == "[Filtered]"
    assert hdrs["X-Anthropic-Api-Key"] == "[Filtered]"
    assert hdrs["User-Agent"] == "Mozilla/5.0"  # untouched
    assert scrubbed["request"]["cookies"]["session"] == "[Filtered]"
    assert scrubbed["request"]["data"]["password"] == "[Filtered]"
    # post_id is fine — non-sensitive, even at error level
    assert scrubbed["request"]["data"]["post_id"] == 42


def test_sentry_before_send_content_redacted_at_error_level():
    """At error level, 'content' fields get redacted so a crashing classifier
    never uploads a user post to Sentry."""
    event = {
        "level": "error",
        "extra": {"content": "user's rant that crashed the classifier"},
    }
    scrubbed = scrub_sensitive_data(event)
    assert scrubbed["extra"]["content"] == "[Redacted]"


def test_sentry_before_send_redacts_content_in_extra_at_info_level():
    """PRE-RELEASE SAFETY: 'content' in event.extra must be redacted even
    at INFO level — we never want user post text to reach Sentry."""
    event = {
        "level": "info",
        "extra": {"content": "a user's post text", "post_id": 42},
    }
    scrubbed = scrub_sensitive_data(event)
    assert scrubbed["extra"]["content"] == "[Redacted]"
    assert scrubbed["extra"]["post_id"] == 42


def test_sentry_before_send_redacts_exception_frame_locals():
    """PRE-RELEASE SAFETY: when the classifier crashes, the local var it
    was processing (usually literally named `content`) must never leak."""
    event = {
        "level": "error",
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "value": "bad",
                    "stacktrace": {
                        "frames": [
                            {
                                "function": "_classify",
                                "vars": {
                                    "content": "the user's private rant",
                                    "text": "same text, different key",
                                    "post_id": 777,
                                    "api_key": "sk-ant-secret",
                                    "harmless_count": 3,
                                },
                            },
                            {
                                "function": "classify_batch",
                                "vars": {
                                    "body": "another post",
                                    "limit": 20,
                                },
                            },
                        ]
                    },
                }
            ]
        },
    }
    scrubbed = scrub_sensitive_data(event)
    frames = scrubbed["exception"]["values"][0]["stacktrace"]["frames"]

    # Frame 0 — content + text redacted, api_key filtered, ids kept
    assert frames[0]["vars"]["content"] == "[Redacted]"
    assert frames[0]["vars"]["text"] == "[Redacted]"
    assert frames[0]["vars"]["api_key"] == "[Filtered]"
    assert frames[0]["vars"]["post_id"] == 777
    assert frames[0]["vars"]["harmless_count"] == 3

    # Frame 1 — body redacted, int kept
    assert frames[1]["vars"]["body"] == "[Redacted]"
    assert frames[1]["vars"]["limit"] == 20


def test_sentry_before_send_survives_malformed_exception_payload():
    """Scrubber must never crash on weird Sentry payload shapes."""
    # Missing keys, unexpected types — none of this should raise.
    event = {
        "exception": {"values": [{"stacktrace": None}, {"stacktrace": {"frames": None}}]},
        "extra": None,
        "request": {"headers": "not-a-dict"},
    }
    scrubbed = scrub_sensitive_data(event)
    assert scrubbed is not None


def test_init_sentry_without_dsn_does_not_crash(monkeypatch, caplog):
    """If SENTRY_DSN_ANNOYANCE is unset, init_sentry returns False and
    logs a warning — it must NEVER raise."""
    monkeypatch.delenv("SENTRY_DSN_ANNOYANCE", raising=False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)

    import observability
    with caplog.at_level(logging.WARNING, logger="annoyance.observability"):
        result = observability.init_sentry(platform="annoyance")
    assert result is False
    # We emit a warning so operators notice.
    assert any("no DSN" in rec.message for rec in caplog.records)
