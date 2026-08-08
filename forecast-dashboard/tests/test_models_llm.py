"""Offline tests for the scored Claude forecaster — anthropic is monkeypatched, no network."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import anthropic
import httpx
import pytest

import db
import models_llm


def _end(days=1):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _market(venue_id="0xa", question="Will X happen by Friday?", **kw):
    m = {
        "uid": f"polymarket:{venue_id}",
        "venue": "polymarket",
        "venue_id": venue_id,
        "question": question,
        "end_date": _end(1),
        "liquidity": 5000.0,
        "baseline_prob": 0.4271,
        "last_price": 0.4271,
        "yes_bid": 0.4183,
        "yes_ask": 0.4359,
    }
    m.update(kw)
    return m


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
    """Patch the anthropic client class in models_llm; responder(kwargs) -> _Resp or raises.

    Returns the list of captured create() kwargs.
    """
    requests = []

    class _Messages:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return responder(kwargs)

    class _Client:
        def __init__(self, **kw):
            self.messages = _Messages()

    monkeypatch.setattr(models_llm.anthropic, "AsyncAnthropic", _Client)
    return requests


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # Same path the conftest `conn` fixture opens, so pre-seeded rows are visible.
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setattr(models_llm, "_auth_logged", False)
    monkeypatch.delenv("FORECAST_LLM_DAILY_CAP", raising=False)
    monkeypatch.setenv("FORECAST_LLM_ENABLED", "1")


# ── gate ─────────────────────────────────────────────────────────────────────


def test_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("FORECAST_LLM_ENABLED", raising=False)

    class _Boom:
        def __init__(self, **kw):
            raise AssertionError("client must not be constructed when the gate is off")

    monkeypatch.setattr(models_llm.anthropic, "AsyncAnthropic", _Boom)
    assert asyncio.run(models_llm.compute([_market()])) == []


def test_gate_requires_exact_1(monkeypatch):
    monkeypatch.setenv("FORECAST_LLM_ENABLED", "true")
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    assert asyncio.run(models_llm.compute([_market()])) == []
    assert requests == []


# ── selection ────────────────────────────────────────────────────────────────


def test_selection_filters(monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    markets = [
        _market("0xmicro", question="Bitcoin up or down at 3:00 PM ET?"),
        _market("0xfar", question="Will Z happen next month?", end_date=_end(5)),
        {"uid": "other:abc", "venue": "other", "venue_id": "abc", "question": "Will W happen?", "end_date": _end(1), "liquidity": 9999.0},
        _market("0xok", question="Will the Senate pass the bill this week?"),
    ]
    rows = asyncio.run(models_llm.compute(markets))
    assert len(requests) == 1
    assert [r["market_uid"] for r in rows] == ["polymarket:0xok"]


def test_cap_prefers_liquidity(monkeypatch):
    monkeypatch.setenv("FORECAST_LLM_DAILY_CAP", "1")
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    markets = [
        _market("0xsmall", question="Will A happen this week?", liquidity=10.0),
        _market("0xbig", question="Will B happen this week?", liquidity=99999.0),
    ]
    rows = asyncio.run(models_llm.compute(markets))
    assert len(requests) == 1
    assert [r["market_uid"] for r in rows] == ["polymarket:0xbig"]


# ── daily cap / dedup ────────────────────────────────────────────────────────


def test_daily_cap_dedup(conn, monkeypatch):
    db.insert_model_prob(conn, {"market_uid": "polymarket:0xa", "source": "llm", "model_prob": 0.5, "prob_method": "llm_forecast", "detail": "{}"})
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    markets = [_market("0xa"), _market("0xb", question="Will Y happen by Saturday?")]
    rows = asyncio.run(models_llm.compute(markets))
    assert len(requests) == 1
    assert "Will Y happen by Saturday?" in requests[0]["messages"][0]["content"]
    assert [r["market_uid"] for r in rows] == ["polymarket:0xb"]


def test_daily_cap_exhausted(conn, monkeypatch):
    monkeypatch.setenv("FORECAST_LLM_DAILY_CAP", "1")
    db.insert_model_prob(conn, {"market_uid": "polymarket:0xearlier", "source": "llm", "model_prob": 0.5, "prob_method": "llm_forecast", "detail": "{}"})
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    assert asyncio.run(models_llm.compute([_market("0xb")])) == []
    assert requests == []


# ── response handling ────────────────────────────────────────────────────────


def test_refusal_skipped(monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _Resp([], stop_reason="refusal"))
    assert asyncio.run(models_llm.compute([_market()])) == []
    assert len(requests) == 1


def test_malformed_json_skipped(monkeypatch):
    _install_client(monkeypatch, lambda kw: _Resp([_Block("text", text="not json {")]))
    assert asyncio.run(models_llm.compute([_market()])) == []


def test_probability_clamped(monkeypatch):
    def responder(kw):
        user = kw["messages"][0]["content"]
        payload = dict(GOOD, probability=1.7 if "Will X" in user else 0.0001)
        return _json_resp(payload)

    _install_client(monkeypatch, responder)
    markets = [
        _market("0xhi", question="Will X happen by Friday?"),
        _market("0xlo", question="Will Y happen by Saturday?"),
    ]
    rows = asyncio.run(models_llm.compute(markets))
    by_uid = {r["market_uid"]: r for r in rows}
    assert by_uid["polymarket:0xhi"]["model_prob"] == 0.99
    assert by_uid["polymarket:0xlo"]["model_prob"] == 0.01


def test_row_contract_last_text_block_and_search_flag(monkeypatch):
    def responder(kw):
        return _Resp(
            [
                _Block("text", text="Let me check recent coverage first."),
                _Block("server_tool_use", name="web_search"),
                _Block("web_search_tool_result"),
                _Block("text", text=json.dumps(GOOD)),
            ]
        )

    _install_client(monkeypatch, responder)
    rows = asyncio.run(models_llm.compute([_market()]))
    assert len(rows) == 1
    r = rows[0]
    assert r["market_uid"] == "polymarket:0xa"
    assert r["source"] == "llm"
    assert r["prob_method"] == "llm_forecast"
    assert r["model_prob"] == 0.7
    detail = json.loads(r["detail"])
    assert detail["model"] == "claude-opus-5"
    assert detail["searched"] is True
    assert detail["confidence"] == "medium"
    assert detail["rationale"] == "base rates say so"
    assert detail["key_factors"] == ["factor one", "factor two"]


def test_pause_turn_continued_once(monkeypatch):
    calls = {"n": 0}

    def responder(kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp([_Block("server_tool_use", name="web_search")], stop_reason="pause_turn")
        assert len(kw["messages"]) == 2
        assert kw["messages"][1]["role"] == "assistant"
        return _json_resp(GOOD)

    requests = _install_client(monkeypatch, responder)
    rows = asyncio.run(models_llm.compute([_market()]))
    assert len(requests) == 2
    assert rows[0]["model_prob"] == 0.7
    assert json.loads(rows[0]["detail"])["searched"] is True


def test_pause_turn_twice_skipped(monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _Resp([], stop_reason="pause_turn"))
    assert asyncio.run(models_llm.compute([_market()])) == []
    assert len(requests) == 2


def test_one_bad_event_does_not_stop_the_pass(monkeypatch):
    def responder(kw):
        if "Will X" in kw["messages"][0]["content"]:
            raise RuntimeError("boom")
        return _json_resp(GOOD)

    _install_client(monkeypatch, responder)
    markets = [
        _market("0xbad", question="Will X happen by Friday?"),
        _market("0xgood", question="Will Y happen by Saturday?"),
    ]
    rows = asyncio.run(models_llm.compute(markets))
    assert [r["market_uid"] for r in rows] == ["polymarket:0xgood"]


def test_no_credentials_aborts_pass_without_requests(monkeypatch):
    requests = []

    class _Messages:
        async def create(self, **kwargs):
            requests.append(kwargs)
            raise AssertionError("no request may be attempted without credentials")

    class _CredentiallessClient:
        # Mirrors the real SDK's resolved-nothing state (construction succeeds,
        # every request would raise TypeError).
        api_key = None
        auth_token = None
        _token_cache = None

        def __init__(self, **kw):
            self.messages = _Messages()

    monkeypatch.setattr(models_llm.anthropic, "AsyncAnthropic", _CredentiallessClient)
    markets = [_market("0xa"), _market("0xb", question="Will Y happen by Saturday?")]
    assert asyncio.run(models_llm.compute(markets)) == []
    assert requests == []


def test_auth_error_returns_empty(monkeypatch):
    def responder(kw):
        raise anthropic.AuthenticationError(
            "invalid x-api-key",
            response=httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")),
            body=None,
        )

    _install_client(monkeypatch, responder)
    markets = [_market("0xa"), _market("0xb", question="Will Y happen by Saturday?")]
    assert asyncio.run(models_llm.compute(markets)) == []


# ── prompt independence ──────────────────────────────────────────────────────


def test_prompt_excludes_baseline(monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    m = _market()
    rows = asyncio.run(models_llm.compute([m]))
    assert len(rows) == 1
    req = requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["max_tokens"] == 2000
    assert req["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    fmt = req["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["required"] == ["probability", "confidence", "rationale", "key_factors"]
    assert fmt["schema"]["additionalProperties"] is False

    sent = json.dumps({"system": req.get("system"), "messages": req["messages"]}, default=str)
    for leak in ("0.4271", "0.4183", "0.4359", "baseline", "liquidity"):
        assert leak not in sent
    assert m["question"] in sent
    assert m["end_date"] in sent


# ── headline grounding ───────────────────────────────────────────────────────


def test_prompt_includes_headlines_block_when_news_exists(monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    monkeypatch.setattr(
        db,
        "news_for",
        lambda conn, uid, limit=8: [
            {"title": "Senate schedules final vote", "source": "The Paper", "url": "https://example.com/a1", "published": "Fri, 07 Aug 2026 10:00:00 GMT", "ts": "2026-08-07T10:00:00Z"},
        ],
    )
    rows = asyncio.run(models_llm.compute([_market()]))
    assert len(rows) == 1
    user = requests[0]["messages"][0]["content"]
    assert "Recent headlines:" in user
    assert "Senate schedules final vote" in user
    assert "The Paper" in user
    assert "https://example.com/a1" not in user
    # The block is context, not a search substitute.
    assert "web search" in user
    sent = json.dumps({"system": requests[0].get("system"), "messages": requests[0]["messages"]}, default=str)
    assert "baseline" not in sent
    assert "0.4271" not in sent


def test_prompt_has_no_headlines_block_without_news(monkeypatch):
    requests = _install_client(monkeypatch, lambda kw: _json_resp(GOOD))
    asyncio.run(models_llm.compute([_market()]))
    assert "Recent headlines:" not in requests[0]["messages"][0]["content"]
