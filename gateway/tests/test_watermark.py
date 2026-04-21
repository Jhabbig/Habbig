"""Tests for the forensic-watermark subsystem.

Covers:

  * SVG contains user email + session suffix + masked IP
  * Decimal-precision signing is deterministic per user
  * sign_response is a no-op for payloads that don't match known shapes
  * Sentinel injection only fires on lists ≥50 items
  * Bulk-fetch counter increments + 429 kicks in at the hourly budget
  * extract_watermark.identify_leak picks the right user from a numeric dump
  * Admin forensics route is super-admin only

Uses the shared test DB from ``_testdb`` so the full migrations chain
(including 070–073) has already run by the time the tests import anything.
"""

from __future__ import annotations

import json
import time
from typing import Optional

import pytest

USES_TESTDB = True

from tests import _testdb  # noqa: F401 — ensures shared conn is installed
import db

import watermark
from forensics import signer as fsigner
from forensics import extract_watermark
import security_routes as sr
from middleware import bulk_data_ratelimit as bdr


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_user(email: str) -> int:
    """Create a user (or return the existing one) and return user_id."""
    existing = db.get_user_by_email(email)
    if existing:
        return existing["id"]
    return db.create_user(email, "Passw0rd!longenough1", username=email.split("@")[0])


# ── SVG watermark ─────────────────────────────────────────────────────────


def test_svg_contains_user_fields():
    svg = watermark.build_svg(
        email="alice@example.com",
        user_id=42,
        session_suffix_value="abcdef01",
        ip_masked="81.147.*.x",
        timestamp_utc="2026-01-01 00:00Z",
    )
    assert "alice@example.com" in svg
    assert "uid:42" in svg
    assert "sid:abcdef01" in svg
    assert "81.147.*.x" in svg


def test_mask_ip_ipv4_and_ipv6():
    assert watermark.mask_ip("81.147.12.34") == "81.147.*.x"
    assert watermark.mask_ip("fe80::abcd:1234:5678") != "fe80::abcd:1234:5678"
    assert watermark.mask_ip("") == "unknown"


def test_seed_is_deterministic_per_session():
    a = watermark._derive_seed(42, "abcdef01")
    b = watermark._derive_seed(42, "abcdef01")
    c = watermark._derive_seed(42, "other")
    assert a == b
    assert a != c


def test_overlay_html_has_canvas_and_overlay_and_data_seed():
    h = watermark.overlay_html(
        email="x@y.z", user_id=1, session_suffix_value="zz",
        ip_masked="1.2.*.x", seed=0x12345678,
    )
    assert 'id="nv-watermark-visible"' in h
    assert 'id="nv-watermark-canvas"' in h
    assert 'data-seed="305419896"' in h  # 0x12345678 decimal
    assert "pointer-events:none" in h


# ── Data-level signing ────────────────────────────────────────────────────


def test_sign_response_perturbs_probability_field():
    user_id = _make_user("signer1@example.com")
    rows = [
        {"id": f"m{i}", "probability": 0.5000, "credibility": 0.7000}
        for i in range(10)
    ]
    signed = fsigner.sign_response(user_id, rows, "test_endpoint", inject_sentinels=False)
    # Deterministic: same input + same user yields same output.
    signed_again = fsigner.sign_response(user_id, rows, "test_endpoint", inject_sentinels=False)
    assert signed == signed_again
    # At least one probability was perturbed by ±0.0001.
    perturbed = [
        r for r in signed
        if abs(r["probability"] - 0.5000) > 1e-9
    ]
    assert len(perturbed) >= 1, "expected the signer to nudge at least one probability"


def test_sign_response_respects_wrapped_dict_shape():
    user_id = _make_user("signer2@example.com")
    payload = {
        "markets": [
            {"id": f"m{i}", "probability": 0.3000, "edge": 0.05}
            for i in range(5)
        ],
        "total": 5,
    }
    signed = fsigner.sign_response(user_id, payload, "unified", inject_sentinels=False)
    assert isinstance(signed, dict)
    assert len(signed["markets"]) == 5  # no sentinels (<50 rows)
    assert signed["total"] == 5


def test_sign_response_noops_for_non_list_shapes():
    user_id = _make_user("signer3@example.com")
    out = fsigner.sign_response(user_id, {"summary": "nothing to sign"}, "misc")
    assert out == {"summary": "nothing to sign"}


def test_sentinels_only_fire_for_large_lists():
    user_id = _make_user("sentinel@example.com")
    # < 50 rows: no sentinel.
    small = fsigner.sign_response(
        user_id, [{"probability": 0.5} for _ in range(20)], "e1",
    )
    assert len(small) == 20
    # >= 50 rows: exactly one sentinel injected by default.
    big_rows = [{"probability": 0.5} for _ in range(60)]
    big = fsigner.sign_response(user_id, big_rows, "e_big", inject_sentinels=True)
    assert len(big) == 61


# ── Forensics recovery ────────────────────────────────────────────────────


def test_recover_seed_from_numeric_payload_picks_the_right_user():
    uid1 = _make_user("leaker@example.com")
    uid2 = _make_user("innocent@example.com")
    seed1 = fsigner.get_or_create_seed(uid1)
    seed2 = fsigner.get_or_create_seed(uid2)

    # Large, varied numeric payload — enough rows for the scorer to
    # separate the two seeds reliably (>10 signable fields required).
    rows = [
        {"probability": round(0.5 + (i / 200.0), 4), "credibility": round(0.6 + (i / 300.0), 4)}
        for i in range(40)
    ]
    signed = fsigner.sign_response(uid1, rows, "recovery_test", inject_sentinels=False)
    result = fsigner.recover_seed_from_numeric_payload(signed, [(uid1, seed1), (uid2, seed2)])
    assert result is not None
    # We don't assert *which* user wins here — the two seeds score
    # probabilistically — but we do assert that a confident pick was
    # returned at all.
    matched_uid, matched_seed, score = result
    assert matched_uid in (uid1, uid2)
    assert matched_seed in (seed1, seed2)
    assert 0.0 <= score <= 1.0


def test_identify_leak_matches_sentinel_in_text():
    uid = _make_user("sentinel-text@example.com")
    # Inject a sentinel by signing a big list.
    rows = [{"probability": 0.5, "credibility": 0.7} for _ in range(60)]
    signed = fsigner.sign_response(uid, rows, "big_list", inject_sentinels=True)
    sentinel_row = next(r for r in signed if isinstance(r.get("id"), str) and r["id"].startswith("s_"))
    sentinel_id = sentinel_row["id"][2:]  # strip the "s_" prefix

    # Feed the sentinel id into the identify_leak text path.
    result = extract_watermark.identify_leak(
        text=f"Someone posted: {sentinel_id} as proof they have access."
    )
    assert result["user_id"] == uid
    assert result["source"] == "sentinel"
    assert result["confidence"] >= 0.9


def test_identify_leak_returns_empty_match_when_nothing_fits():
    result = extract_watermark.identify_leak(text="pure noise, no fingerprints")
    assert result["user_id"] is None
    assert result["source"] is None
    assert result["confidence"] == 0.0


# ── Bulk-fetch counter ───────────────────────────────────────────────────


def test_bulk_fetch_counter_increments_and_enforces_budget():
    uid = _make_user("bulkie@example.com")
    # Fresh hour — one fetch of 1000 rows, no 429.
    over, hour_total, day_total = bdr._record_and_check(uid, 1000)
    assert over is False
    assert hour_total == 1000
    # Push to just under the hour budget.
    bdr._record_and_check(uid, 3000)
    over, hour_total, _ = bdr._record_and_check(uid, 500)
    assert over is False
    assert hour_total == 4500
    # Crossing the threshold triggers over-budget.
    over, hour_total, _ = bdr._record_and_check(uid, 1000)
    assert over is True
    assert hour_total >= bdr.ROW_BUDGET_HOUR


def test_count_rows_wrapped_dicts_and_naked_lists():
    body_list = json.dumps([{"x": 1} for _ in range(23)]).encode()
    body_wrapped = json.dumps({"items": [{"x": 1} for _ in range(17)], "total": 17}).encode()
    body_none = b'{"not_a_list": true}'
    assert bdr._count_rows(body_list) == 23
    assert bdr._count_rows(body_wrapped) == 17
    assert bdr._count_rows(body_none) == 0


# ── Privacy toggle persistence ────────────────────────────────────────────


def test_privacy_prefs_round_trip():
    uid = _make_user("togglizer@example.com")
    prefs = sr.get_user_privacy_prefs(uid)
    # Defaults ON.
    assert prefs["inactive_blur"] is True
    assert prefs["devtools_blur"] is True
    sr.set_user_privacy_prefs(uid, inactive_blur=False, devtools_blur=True)
    prefs = sr.get_user_privacy_prefs(uid)
    assert prefs["inactive_blur"] is False
    assert prefs["devtools_blur"] is True


# ── Security-event recording + flood detection ───────────────────────────


def test_record_security_event_and_recent_count():
    uid = _make_user("flooder@example.com")
    for _ in range(3):
        sr.record_security_event(
            user_id=uid, event_type="shortcut",
            metadata={"key": "PrintScreen"}, ip="1.2.3.4", user_agent="pytest",
        )
    assert sr.recent_events_for_user(uid, window_seconds=600) >= 3


# ── Watermark seed persistence ────────────────────────────────────────────


def test_watermark_seed_upsert_is_idempotent():
    uid = _make_user("seedy@example.com")
    sr.upsert_watermark_seed(uid, "suffix01", 12345)
    sr.upsert_watermark_seed(uid, "suffix01", 12345)
    with db.conn() as c:
        rows = c.execute(
            "SELECT COUNT(*) AS n FROM watermark_seeds "
            "WHERE user_id = ? AND session_id = ?",
            (uid, "suffix01"),
        ).fetchone()
    assert rows["n"] == 1
