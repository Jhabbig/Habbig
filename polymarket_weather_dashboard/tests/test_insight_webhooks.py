"""Tests for the insight webhook dispatcher.

Covers the filter logic, the three payload formatters, the failure-
tracking + auto-disable behavior, and the per-user scoping on the
CRUD helpers. HTTP calls are stubbed via `requests` monkeypatching;
no actual outbound traffic.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from unittest.mock import MagicMock

import pytest

import insight_webhooks as iwh


WEBHOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS insight_webhooks (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                  TEXT NOT NULL,
    url                      TEXT NOT NULL,
    kind                     TEXT NOT NULL,
    min_confidence           TEXT NOT NULL DEFAULT 'medium',
    min_abs_edge             REAL NOT NULL DEFAULT 0.10,
    recommendation_filter    TEXT DEFAULT '',
    enabled                  INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_fired_at            TEXT,
    last_error               TEXT,
    consecutive_failures     INTEGER NOT NULL DEFAULT 0,
    total_fires              INTEGER NOT NULL DEFAULT 0
);
"""


def _make_factory():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(WEBHOOK_SCHEMA)
    lock = threading.Lock()

    @contextlib.contextmanager
    def factory(readonly=False):
        with lock:
            try:
                yield conn
                if not readonly:
                    conn.commit()
            except Exception:
                if not readonly:
                    conn.rollback()
                raise

    return factory, conn


def _row(**overrides):
    """Sample insight row matching the shape `dispatch` expects."""
    base = {
        "id": 1, "market_id": "m1", "recommendation": "BUY_YES",
        "confidence": "high", "headline": "buy yes",
        "edge": 0.15, "suggested_limit_cents": 60,
        "tail_warning": 0, "triggered_by": "user",
    }
    base.update(overrides)
    return base


def _hook(**overrides):
    """Sample webhook config matching the shape `should_fire` expects."""
    base = {
        "id": 1, "user_id": "u1", "url": "https://example.com/hook",
        "kind": "discord", "enabled": 1,
        "min_confidence": "medium", "min_abs_edge": 0.10,
        "recommendation_filter": "",
    }
    base.update(overrides)
    return base


# ─── Filter logic ─────────────────────────────────────────────────────────────

def test_should_fire_passes_when_all_filters_clear():
    assert iwh.should_fire(_hook(), _row()) is True


def test_should_fire_blocks_when_disabled():
    assert iwh.should_fire(_hook(enabled=0), _row()) is False


def test_should_fire_blocks_below_min_confidence():
    # Hook requires medium, insight is low → block
    assert iwh.should_fire(
        _hook(min_confidence="medium"), _row(confidence="low")
    ) is False


def test_should_fire_blocks_below_min_edge():
    assert iwh.should_fire(
        _hook(min_abs_edge=0.10), _row(edge=0.04)
    ) is False
    # Negative edges also count by absolute value
    assert iwh.should_fire(
        _hook(min_abs_edge=0.10), _row(edge=-0.15)
    ) is True


def test_should_fire_recommendation_filter():
    """Only fires when the row's recommendation is in the allowlist."""
    hook = _hook(recommendation_filter="BUY_YES, BUY_NO")
    assert iwh.should_fire(hook, _row(recommendation="BUY_YES")) is True
    assert iwh.should_fire(hook, _row(recommendation="PASS")) is False


def test_should_fire_empty_filter_means_all():
    assert iwh.should_fire(
        _hook(recommendation_filter=""), _row(recommendation="WAIT_AND_SEE")
    ) is True


# ─── Payload formatters ───────────────────────────────────────────────────────

def test_format_discord_includes_recommendation_color_and_url():
    payload = iwh.format_discord(_row(), market_url="https://example.com/m1")
    assert "embeds" in payload
    embed = payload["embeds"][0]
    assert "BUY YES" in embed["title"]
    assert embed["color"] == 0x4DD0A8  # accent green for BUY_YES
    assert embed["url"] == "https://example.com/m1"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert "+15.0pp" in fields["Edge"]
    assert fields["Confidence"] == "high"
    assert fields["Limit"] == "60¢"


def test_format_discord_adds_tail_warning_field():
    payload = iwh.format_discord(_row(tail_warning=1))
    embed = payload["embeds"][0]
    field_names = [f["name"] for f in embed["fields"]]
    assert "Tail risk" in field_names


def test_format_slack_includes_market_url_block_when_provided():
    payload = iwh.format_slack(_row(), market_url="https://example.com/m1")
    text_blocks = [b["text"]["text"] for b in payload["blocks"]
                   if b.get("type") == "section"]
    assert any("View market" in t for t in text_blocks)


def test_format_generic_includes_full_row():
    payload = iwh.format_generic(_row(), market_url="https://example.com/m1")
    assert payload["recommendation"] == "BUY_YES"
    assert payload["market_url"] == "https://example.com/m1"


# ─── CRUD helpers ─────────────────────────────────────────────────────────────

def test_create_and_list_webhook_round_trip():
    factory, _ = _make_factory()
    wid = iwh.create_webhook(
        factory, user_id="u1", url="https://example.com/hook",
        kind="discord", min_confidence="high", min_abs_edge=0.20,
        recommendation_filter="BUY_YES,BUY_NO",
    )
    assert wid > 0
    hooks = iwh.list_webhooks(factory, "u1")
    assert len(hooks) == 1
    assert hooks[0]["url"] == "https://example.com/hook"
    assert hooks[0]["min_confidence"] == "high"
    assert hooks[0]["min_abs_edge"] == 0.20


def test_get_webhook_scoped_to_user():
    """A user can only fetch their own webhooks."""
    factory, _ = _make_factory()
    wid = iwh.create_webhook(factory, user_id="alice",
                              url="https://x.com/h", kind="generic")
    assert iwh.get_webhook(factory, wid, "alice") is not None
    assert iwh.get_webhook(factory, wid, "bob") is None


def test_update_webhook_only_allowed_fields():
    """update_webhook silently drops any kwarg not in the allowlist
    so callers can't smuggle in `url`, `kind`, or other immutable
    columns through the **fields path."""
    factory, _ = _make_factory()
    wid = iwh.create_webhook(factory, user_id="u1",
                              url="https://x.com/h", kind="generic")
    changed = iwh.update_webhook(factory, wid, "u1",
                                  enabled=0, min_confidence="low",
                                  url="https://hax.com/h",  # not allowed
                                  kind="discord")            # not allowed
    assert changed
    row = iwh.get_webhook(factory, wid, "u1")
    assert row["enabled"] == 0
    assert row["min_confidence"] == "low"
    # The disallowed fields should have been silently dropped
    assert row["url"] == "https://x.com/h"
    assert row["kind"] == "generic"
    assert row["user_id"] == "u1"


def test_update_webhook_rejects_other_users():
    factory, _ = _make_factory()
    wid = iwh.create_webhook(factory, user_id="alice",
                              url="https://x.com/h", kind="generic")
    assert iwh.update_webhook(factory, wid, "bob", enabled=0) is False


def test_delete_webhook_scoped_to_user():
    factory, _ = _make_factory()
    wid = iwh.create_webhook(factory, user_id="alice",
                              url="https://x.com/h", kind="generic")
    assert iwh.delete_webhook(factory, wid, "bob") is False
    assert iwh.delete_webhook(factory, wid, "alice") is True
    assert iwh.get_webhook(factory, wid, "alice") is None


# ─── Fire path (mocked HTTP) ──────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def test_fire_ok_path_records_success(monkeypatch):
    factory, conn = _make_factory()
    wid = iwh.create_webhook(factory, user_id="u1",
                              url="https://x.com/h", kind="generic")
    post = MagicMock(return_value=_FakeResponse(204))
    monkeypatch.setattr(iwh.requests, "post", post)

    result = iwh.fire(_hook(id=wid), _row(), conn_factory=factory)
    assert result["ok"] is True
    assert result["status"] == 204
    row = iwh.get_webhook(factory, wid, "u1")
    assert row["consecutive_failures"] == 0
    assert row["total_fires"] == 1
    assert row["last_error"] is None


def test_fire_non_2xx_records_failure(monkeypatch):
    factory, _ = _make_factory()
    wid = iwh.create_webhook(factory, user_id="u1",
                              url="https://x.com/h", kind="generic")
    monkeypatch.setattr(iwh.requests, "post",
                        MagicMock(return_value=_FakeResponse(500)))
    result = iwh.fire(_hook(id=wid), _row(), conn_factory=factory)
    assert result["ok"] is False
    assert result["status"] == 500
    row = iwh.get_webhook(factory, wid, "u1")
    assert row["consecutive_failures"] == 1
    assert "500" in (row["last_error"] or "")


def test_fire_network_exception_records_failure(monkeypatch):
    factory, _ = _make_factory()
    wid = iwh.create_webhook(factory, user_id="u1",
                              url="https://x.com/h", kind="generic")
    def boom(*a, **kw):
        raise iwh.requests.ConnectionError("dns failed")
    monkeypatch.setattr(iwh.requests, "post", boom)
    result = iwh.fire(_hook(id=wid), _row(), conn_factory=factory)
    assert result["ok"] is False
    row = iwh.get_webhook(factory, wid, "u1")
    assert row["consecutive_failures"] == 1


def test_fire_auto_disables_after_threshold(monkeypatch):
    factory, conn = _make_factory()
    wid = iwh.create_webhook(factory, user_id="u1",
                              url="https://x.com/h", kind="generic")
    monkeypatch.setattr(iwh.requests, "post",
                        MagicMock(return_value=_FakeResponse(500)))
    # Trip threshold worth of failures in a row
    for _ in range(iwh.AUTO_DISABLE_THRESHOLD):
        iwh.fire(_hook(id=wid), _row(), conn_factory=factory)
    row = iwh.get_webhook(factory, wid, "u1")
    assert row["enabled"] == 0
    assert row["consecutive_failures"] >= iwh.AUTO_DISABLE_THRESHOLD


def test_fire_success_resets_failure_counter(monkeypatch):
    factory, _ = _make_factory()
    wid = iwh.create_webhook(factory, user_id="u1",
                              url="https://x.com/h", kind="generic")
    # Two failures then a success
    monkeypatch.setattr(iwh.requests, "post",
                        MagicMock(return_value=_FakeResponse(500)))
    iwh.fire(_hook(id=wid), _row(), conn_factory=factory)
    iwh.fire(_hook(id=wid), _row(), conn_factory=factory)
    monkeypatch.setattr(iwh.requests, "post",
                        MagicMock(return_value=_FakeResponse(200)))
    iwh.fire(_hook(id=wid), _row(), conn_factory=factory)
    row = iwh.get_webhook(factory, wid, "u1")
    assert row["consecutive_failures"] == 0
    assert row["last_error"] is None


# ─── dispatch ─────────────────────────────────────────────────────────────────

def test_dispatch_only_fires_matching_enabled_webhooks(monkeypatch):
    factory, _ = _make_factory()
    iwh.create_webhook(factory, user_id="u1", url="https://x.com/a",
                        kind="generic", min_confidence="high",
                        min_abs_edge=0.10)
    iwh.create_webhook(factory, user_id="u1", url="https://x.com/b",
                        kind="generic", min_confidence="high",
                        min_abs_edge=0.50)  # too tight
    iwh.create_webhook(factory, user_id="u2", url="https://x.com/c",
                        kind="generic", min_confidence="low",
                        min_abs_edge=0.0,
                        recommendation_filter="PASS")  # wrong rec

    posts = []
    monkeypatch.setattr(iwh.requests, "post",
                        lambda url, **kw: posts.append(url) or _FakeResponse(200))
    n = iwh.dispatch(factory, _row(edge=0.15, confidence="high",
                                    recommendation="BUY_YES"))
    assert n == 1
    assert posts == ["https://x.com/a"]


def test_dispatch_skips_disabled_webhooks(monkeypatch):
    factory, conn = _make_factory()
    wid = iwh.create_webhook(factory, user_id="u1", url="https://x.com/h",
                              kind="generic")
    conn.execute("UPDATE insight_webhooks SET enabled = 0 WHERE id = ?", (wid,))
    conn.commit()
    posts = []
    monkeypatch.setattr(iwh.requests, "post",
                        lambda url, **kw: posts.append(url) or _FakeResponse(200))
    n = iwh.dispatch(factory, _row())
    assert n == 0
    assert posts == []
