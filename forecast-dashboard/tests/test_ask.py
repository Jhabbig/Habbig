"""Offline tests for the ask engine and the custom-question adjudicator — anthropic is monkeypatched, no network."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import anthropic
import httpx
import pytest

import ask
import db
import models_llm
import resolver


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


GOOD = {"probability": 0.7, "confidence": "medium", "rationale": "base rates say so", "key_factors": ["factor one", "factor two"]}


def _json_resp(payload, stop_reason="end_turn"):
    return _Resp([_Block("text", text=json.dumps(payload))], stop_reason)


def _install_client(monkeypatch, responder):
    """Patch anthropic.AsyncAnthropic (shared by ask + resolver); returns captured create() kwargs."""
    requests = []

    class _Messages:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return responder(kwargs)

    class _Client:
        def __init__(self, **kw):
            self.messages = _Messages()

    monkeypatch.setattr(ask.anthropic, "AsyncAnthropic", _Client)
    return requests


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # Same path the conftest `conn` fixture opens, so pre-seeded rows are visible.
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setenv("FORECAST_LLM_ENABLED", "1")


def _seed_board_market(conn, venue_id="0xa", question="Will the Senate pass the bill this week?"):
    uid = db.upsert_market(
        conn,
        {
            "venue": "polymarket",
            "venue_id": venue_id,
            "question": question,
            "category": "politics",
            "url": "https://example.com/m",
            "end_date": _iso(datetime.now(timezone.utc) + timedelta(days=3)),
        },
    )
    db.insert_snapshot(
        conn,
        {"market_uid": uid, "yes_bid": 0.41, "yes_ask": 0.43, "last_price": 0.42, "baseline_prob": 0.42, "spread": 0.02, "liquidity": 5000.0, "volume_24h": 1000.0},
    )
    return uid


def _seed_custom(conn, question="Will the ribbon be cut on the new bridge?", days_past=2, with_forecast=True, with_calibration=True):
    end = _iso(datetime.now(timezone.utc) - timedelta(days=days_past))
    uid = db.create_custom_market(conn, question, end)
    if with_forecast:
        db.insert_model_prob(conn, {"market_uid": uid, "source": "llm", "model_prob": 0.7, "prob_method": "llm_ask", "detail": "{}"})
    if with_calibration:
        db.log_calibration_row(
            conn,
            {
                "market_uid": uid,
                "platform": "custom",
                "category": "custom",
                "target_date": end,
                "source": "llm",
                "model_prob": 0.7,
                "market_prob": None,
                "displayed_at": "2026-08-01",
                "prob_method": "llm_ask",
            },
        )
    return uid


# ── ask: matches-only path (LLM disabled) ────────────────────────────────────


def test_disabled_returns_matches_only(conn, monkeypatch):
    monkeypatch.delenv("FORECAST_LLM_ENABLED", raising=False)
    _seed_board_market(conn)

    class _Boom:
        def __init__(self, **kw):
            raise AssertionError("client must not be constructed when the gate is off")

    monkeypatch.setattr(ask.anthropic, "AsyncAnthropic", _Boom)
    out = asyncio.run(ask.ask("senate pass the bill"))
    assert out["llm"] is None
    assert "FORECAST_LLM_ENABLED=1" in out["note"]
    assert [m["uid"] for m in out["matches"]] == ["polymarket:0xa"]
    assert conn.execute("SELECT COUNT(*) FROM markets WHERE venue = 'custom'").fetchone()[0] == 0


def test_match_rows_are_board_shaped(conn, monkeypatch):
    monkeypatch.delenv("FORECAST_LLM_ENABLED", raising=False)
    uid = _seed_board_market(conn)
    db.insert_model_prob(conn, {"market_uid": uid, "source": "weather", "model_prob": 0.66, "prob_method": "ensemble", "detail": "{}"})

    out = asyncio.run(ask.ask("senate pass the bill"))
    (row,) = out["matches"]
    expected_keys = {
        "uid",
        "venue",
        "question",
        "category",
        "url",
        "end_date",
        "display_prob",
        "baseline_prob",
        "model_source",
        "model_prob",
        "move_24h",
        "new",
        "spread",
        "confidence",
        "liquidity",
        "volume_24h",
        "disagreement",
        "resolved",
        "outcome",
        "resolved_at",
        "snapshot_ts",
    }
    assert expected_keys <= set(row)
    assert row["venue"] == "polymarket"
    assert row["url"] == "https://example.com/m"
    assert row["end_date"]
    assert row["baseline_prob"] == 0.42
    assert row["model_source"] == "weather"
    assert row["model_prob"] == 0.66
    assert row["display_prob"] == 0.66
    assert row["confidence"] == "high"


# ── ask: custom market + cache ───────────────────────────────────────────────


def test_custom_upsert_dedupe_and_6h_cache(conn, monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    q = "Will the new rail line open by 2026-12-31?"

    first = asyncio.run(ask.ask(q))
    assert len(requests) == 1
    assert first["llm"]["probability"] == 0.7
    assert first["llm"]["uid"].startswith("custom:")

    second = asyncio.run(ask.ask(q))
    assert len(requests) == 1
    assert "Cached" in second["note"]
    assert second["llm"]["uid"] == first["llm"]["uid"]
    assert second["llm"]["probability"] == 0.7
    assert second["llm"]["rationale"] == "base rates say so"

    assert conn.execute("SELECT COUNT(*) FROM markets WHERE venue = 'custom'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM model_probs WHERE source = 'llm'").fetchone()[0] == 1


def test_cache_expires_after_6h(conn, monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    q = "Will the new rail line open by 2026-12-31?"
    asyncio.run(ask.ask(q))
    stale = _iso(datetime.now(timezone.utc) - timedelta(hours=7))
    conn.execute("UPDATE model_probs SET ts = ?", (stale,))
    conn.commit()
    asyncio.run(ask.ask(q))
    assert len(requests) == 2
    assert conn.execute("SELECT COUNT(*) FROM markets WHERE venue = 'custom'").fetchone()[0] == 1


def test_reask_without_deadline_preserves_stored_end_date(conn, monkeypatch):
    _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    end = _iso(datetime.now(timezone.utc) + timedelta(days=30))
    q = "Will the observatory see first light soon?"
    uid = db.create_custom_market(conn, q, end)
    out = asyncio.run(ask.ask(q))
    assert out["llm"]["uid"] == uid
    assert db.get_market(conn, uid)["end_date"] == end
    assert end[:10] in out["note"]


# ── ask: deadline parsing ────────────────────────────────────────────────────

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Will X launch by 2027-03-03?", "2027-03-03T23:59:59Z"),
        ("Will X launch by March 3, 2027?", "2027-03-03T23:59:59Z"),
        ("Will X launch on the 3rd of March 2027?", "2027-03-03T23:59:59Z"),
        ("Will X launch by March 3?", "2027-03-03T23:59:59Z"),
        ("Will X launch by September 1?", "2026-09-01T23:59:59Z"),
        ("Will X launch in March 2027?", "2027-03-31T23:59:59Z"),
        ("Will X be announced by March?", "2027-03-31T23:59:59Z"),
        ("Will X be announced this week?", "2026-08-09T23:59:59Z"),
        ("Will X be announced next week?", "2026-08-16T23:59:59Z"),
        ("Will X ship this month?", "2026-08-31T23:59:59Z"),
        ("Will X ship next month?", "2026-09-30T23:59:59Z"),
        ("Will X happen before year-end?", "2026-12-31T23:59:59Z"),
        ("Will X be confirmed tomorrow?", "2026-08-09T23:59:59Z"),
        ("Will humans settle the ocean floor?", None),
        ("Will the committee's report change anything?", None),
    ],
)
def test_parse_deadline(question, expected):
    assert ask.parse_deadline(question, now=_NOW) == expected


# ── ask: notes, prompt, storage ──────────────────────────────────────────────


def test_deadline_note_promises_autocheck(conn, monkeypatch):
    _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    out = asyncio.run(ask.ask("Will the new rail line open by 2026-12-31?"))
    assert "2026-12-31" in out["note"]
    assert "checked automatically" in out["note"]
    uid = out["llm"]["uid"]
    assert db.get_market(conn, uid)["end_date"] == "2026-12-31T23:59:59Z"
    cal = conn.execute("SELECT * FROM calibration WHERE market_uid = ?", (uid,)).fetchall()
    assert len(cal) == 1
    assert cal[0]["source"] == "llm"
    assert cal[0]["prob_method"] == "llm_ask"
    assert cal[0]["market_prob"] is None
    assert cal[0]["outcome"] is None


def test_no_deadline_note_is_honest(conn, monkeypatch):
    _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    out = asyncio.run(ask.ask("Will humans settle the ocean floor?"))
    assert out["llm"] is not None
    assert "cannot be tracked for accuracy" in out["note"]
    uid = out["llm"]["uid"]
    assert db.get_market(conn, uid)["end_date"] is None
    # No deadline -> nothing to adjudicate -> no calibration row either.
    assert conn.execute("SELECT COUNT(*) FROM calibration WHERE market_uid = ?", (uid,)).fetchone()[0] == 0


def test_prompt_contract_and_stored_row(conn, monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    out = asyncio.run(ask.ask("Will the new rail line open by 2026-12-31?"))
    req = requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    fmt = req["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] == models_llm.OUTPUT_SCHEMA
    user = req["messages"][0]["content"]
    assert datetime.now(timezone.utc).date().isoformat() in user
    assert "2026-12-31T23:59:59Z" in user

    uid = out["llm"]["uid"]
    row = conn.execute("SELECT * FROM model_probs WHERE market_uid = ?", (uid,)).fetchone()
    assert row["source"] == "llm"
    assert row["prob_method"] == "llm_ask"
    assert row["model_prob"] == 0.7
    assert json.loads(row["detail"])["model"] == "claude-opus-5"


def test_refusal_yields_no_forecast(conn, monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _Resp([], stop_reason="refusal"))
    out = asyncio.run(ask.ask("Will the new rail line open by 2026-12-31?"))
    assert out["llm"] is None
    assert "declined" in out["note"]
    assert len(requests) == 1
    assert conn.execute("SELECT COUNT(*) FROM model_probs").fetchone()[0] == 0


def test_custom_markets_get_no_snapshot_and_stay_off_the_board(conn, monkeypatch):
    _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    _seed_board_market(conn)
    out = asyncio.run(ask.ask("Will the new rail line open by 2026-12-31?"))
    uid = out["llm"]["uid"]
    assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE market_uid = ?", (uid,)).fetchone()[0] == 0
    board_uids = {r["uid"] for r in db.latest_snapshots(conn, 365)}
    assert "polymarket:0xa" in board_uids
    assert uid not in board_uids
    assert db.get_market(conn, uid) is not None


# ── adjudicator ──────────────────────────────────────────────────────────────


def test_adjudicate_gate_off(conn, monkeypatch):
    monkeypatch.delenv("FORECAST_LLM_ENABLED", raising=False)
    _seed_custom(conn)

    class _Boom:
        def __init__(self, **kw):
            raise AssertionError("client must not be constructed when the gate is off")

    monkeypatch.setattr(ask.anthropic, "AsyncAnthropic", _Boom)
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 0


def test_adjudicate_yes_resolves_and_backfills(conn, monkeypatch):
    uid = _seed_custom(conn)
    requests = _install_client(monkeypatch, lambda kw: _json_resp({"outcome": "yes", "evidence": "local paper covered the opening"}))
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 1
    assert len(requests) == 1
    m = db.get_market(conn, uid)
    assert m["resolved"] == 1
    assert m["active"] == 0
    assert m["outcome"] == 1
    cal = conn.execute("SELECT outcome, resolved_at FROM calibration WHERE market_uid = ?", (uid,)).fetchone()
    assert cal["outcome"] == 1
    assert cal["resolved_at"]
    user = requests[0]["messages"][0]["content"]
    assert "ribbon" in user
    assert m["end_date"] in user


def test_adjudicate_no_resolves_zero(conn, monkeypatch):
    uid = _seed_custom(conn)
    _install_client(monkeypatch, lambda kw: _json_resp({"outcome": "no", "evidence": "the opening was postponed"}))
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 1
    m = db.get_market(conn, uid)
    assert m["resolved"] == 1
    assert m["outcome"] == 0
    assert conn.execute("SELECT outcome FROM calibration WHERE market_uid = ?", (uid,)).fetchone()["outcome"] == 0


def test_adjudicate_unclear_leaves_pending_then_resolves(conn, monkeypatch):
    uid = _seed_custom(conn)
    _install_client(monkeypatch, lambda kw: _json_resp({"outcome": "unclear", "evidence": "no coverage found yet"}))
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 0
    m = db.get_market(conn, uid)
    assert m["resolved"] == 0
    assert m["active"] == 1
    assert conn.execute("SELECT outcome FROM calibration WHERE market_uid = ?", (uid,)).fetchone()["outcome"] is None

    _install_client(monkeypatch, lambda kw: _json_resp({"outcome": "yes", "evidence": "confirmed a day later"}))
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 1
    assert db.get_market(conn, uid)["resolved"] == 1


def test_adjudicate_refusal_leaves_pending(conn, monkeypatch):
    uid = _seed_custom(conn)
    requests = _install_client(monkeypatch, lambda kw: _Resp([], stop_reason="refusal"))
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 0
    assert len(requests) == 1
    assert db.get_market(conn, uid)["resolved"] == 0


def test_adjudicate_gives_up_after_7_days(conn, monkeypatch):
    uid = _seed_custom(conn, days_past=8)
    requests = _install_client(monkeypatch, lambda kw: _json_resp({"outcome": "yes", "evidence": "should never be asked"}))
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 0
    assert requests == []
    m = db.get_market(conn, uid)
    assert m["active"] == 0
    assert m["outcome"] is None
    # Give-up mirrors the void paths: unscored calibration rows are dropped
    # entirely so an unresolvable question can never poison the ledger.
    assert conn.execute("SELECT COUNT(*) AS n FROM calibration WHERE market_uid = ?", (uid,)).fetchone()["n"] == 0
    # Dropped from the pending set: the next pass doesn't reconsider it.
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 0
    assert requests == []


def test_adjudicate_cap_10_per_pass(conn, monkeypatch):
    for i in range(12):
        _seed_custom(conn, question=f"Will distinct event number {i} happen on schedule?", with_calibration=False)
    requests = _install_client(monkeypatch, lambda kw: _json_resp({"outcome": "unclear", "evidence": "n/a"}))
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 0
    assert len(requests) == resolver.ADJUDICATE_CAP == 10


def test_adjudicate_skips_noncustom_and_forecastless(conn, monkeypatch):
    no_forecast_uid = _seed_custom(conn, question="Will the forgotten question resolve itself?", with_forecast=False)
    poly_uid = db.upsert_market(
        conn,
        {"venue": "polymarket", "venue_id": "0xpast", "question": "Will P happen?", "end_date": _iso(datetime.now(timezone.utc) - timedelta(days=2))},
    )
    db.insert_model_prob(conn, {"market_uid": poly_uid, "source": "llm", "model_prob": 0.5, "prob_method": "llm_forecast", "detail": "{}"})
    requests = _install_client(monkeypatch, lambda kw: _json_resp({"outcome": "yes", "evidence": "x"}))
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 0
    assert requests == []
    assert db.get_market(conn, no_forecast_uid)["resolved"] == 0
    assert db.get_market(conn, poly_uid)["resolved"] == 0


def test_adjudicate_auth_error_aborts_pass(conn, monkeypatch):
    _seed_custom(conn, question="Will event A happen on schedule?", with_calibration=False)
    _seed_custom(conn, question="Will event B happen on schedule?", with_calibration=False)

    def responder(kw):
        raise anthropic.AuthenticationError(
            "invalid x-api-key",
            response=httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")),
            body=None,
        )

    requests = _install_client(monkeypatch, responder)
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 0
    assert len(requests) == 1


def test_adjudicate_one_bad_market_never_stops_the_pass(conn, monkeypatch):
    _seed_custom(conn, question="Will event A happen on schedule?", with_calibration=False)
    uid_b = _seed_custom(conn, question="Will event B happen on schedule?", with_calibration=False)

    def responder(kw):
        if "event A" in kw["messages"][0]["content"]:
            raise RuntimeError("boom")
        return _json_resp({"outcome": "yes", "evidence": "x"})

    _install_client(monkeypatch, responder)
    assert asyncio.run(resolver.adjudicate_custom(conn)) == 1
    assert db.get_market(conn, uid_b)["resolved"] == 1


# ── no advice language, anywhere ─────────────────────────────────────────────


def test_no_advice_language_in_any_prompt(conn, monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    asyncio.run(ask.ask("Will the new rail line open by 2026-12-31?"))
    _seed_custom(conn)
    adjudicate_requests = _install_client(monkeypatch, lambda kw: _json_resp({"outcome": "unclear", "evidence": "n/a"}))
    asyncio.run(resolver.adjudicate_custom(conn))

    all_requests = requests + adjudicate_requests
    assert len(all_requests) >= 2
    advice_phrases = ("you should buy", "you should sell", "buy now", "sell now", "place a bet", "good bet", "trade this", "investment opportunity", "position size")
    for req in all_requests:
        assert "NEVER give trading or investment advice" in req["system"]
        sent = json.dumps({"system": req.get("system"), "messages": req["messages"]}, default=str).lower()
        for phrase in advice_phrases:
            assert phrase not in sent
