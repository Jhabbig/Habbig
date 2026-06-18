"""Tests for the live Claude adapter (intelligence/narve_llm).

No network: the live path is exercised by monkeypatching ai.client.call_claude,
and the offline heuristic path runs end-to-end through info_forecast with
NARVE_OFFLINE=1. Verifies the adapter (a) never silently returns 0.5, (b)
forwards the correct feature/model/max_tokens, and (c) the offline heuristic is
clearly labelled and produces a valid forecast dict.
"""
import json

import pytest

from intelligence import narve_llm as nl
from intelligence import info_forecast


# ── make_llm wiring shape ────────────────────────────────────────────────────

def test_make_llm_returns_callable(monkeypatch):
    monkeypatch.delenv("NARVE_OFFLINE", raising=False)
    llm = nl.make_llm()
    assert callable(llm)


def test_make_llm_offline_returns_callable(monkeypatch):
    monkeypatch.setenv("NARVE_OFFLINE", "1")
    llm = nl.make_llm()
    assert callable(llm)


def test_model_env_override(monkeypatch):
    monkeypatch.delenv("NARVE_FORECAST_MODEL", raising=False)
    assert nl._model() == nl.DEFAULT_FORECAST_MODEL
    monkeypatch.setenv("NARVE_FORECAST_MODEL", "claude-opus-4-8")
    assert nl._model() == "claude-opus-4-8"


# ── Live adapter: routes through call_claude, never fabricates ────────────────

def test_live_llm_forwards_correct_args(monkeypatch):
    """The live callable must call ai.client.call_claude with the forecast
    feature, the configured model, and the forecast max_tokens — and return its
    text verbatim."""
    monkeypatch.delenv("NARVE_OFFLINE", raising=False)
    monkeypatch.setenv("NARVE_FORECAST_MODEL", "claude-test-model")

    captured = {}

    async def fake_call_claude(**kwargs):
        captured.update(kwargs)
        return '{"probability_yes": 0.61, "confidence": 0.7, "digest": "ok", ' \
               '"drivers": [], "political_lean": "center"}'

    from ai import client as ai_client
    monkeypatch.setattr(ai_client, "call_claude", fake_call_claude)

    llm = nl.make_llm()
    out = llm("SYS", "USER")

    assert json.loads(out)["probability_yes"] == 0.61
    assert captured["feature"] == nl.FORECAST_FEATURE
    assert captured["model"] == "claude-test-model"
    assert captured["max_tokens"] == nl.FORECAST_MAX_TOKENS
    assert captured["system"] == "SYS"
    assert captured["user"] == "USER"


def test_live_llm_raises_on_none_never_returns_half(monkeypatch):
    """call_claude returns None on kill-switch / missing key / API error. The
    adapter must raise a clear RuntimeError, NOT return 0.5 or any forecast."""
    monkeypatch.delenv("NARVE_OFFLINE", raising=False)

    async def fake_call_claude(**kwargs):
        return None

    from ai import client as ai_client
    monkeypatch.setattr(ai_client, "call_claude", fake_call_claude)

    llm = nl.make_llm()
    with pytest.raises(RuntimeError) as exc:
        llm("SYS", "USER")
    msg = str(exc.value).lower()
    assert "no text" in msg or "kill-switch" in msg
    assert "0.5" not in str(exc.value)  # we never name a fabricated probability


def test_forecast_event_live_path(monkeypatch):
    """forecast_event wires make_llm into info_forecast.forecast end to end."""
    monkeypatch.delenv("NARVE_OFFLINE", raising=False)

    async def fake_call_claude(**kwargs):
        return json.dumps({
            "probability_yes": 0.43, "confidence": 0.6,
            "digest": "live", "drivers": ["d"], "political_lean": "center",
        })

    from ai import client as ai_client
    monkeypatch.setattr(ai_client, "call_claude", fake_call_claude)

    items = [{"platform": "reddit", "text": "ground report"},
             {"platform": "news", "text": "wire story"}]
    out = nl.forecast_event("Will it happen?", items)
    assert out["probability_yes"] == 0.43
    assert out["n_items"] == 2
    assert set(out["source_spread"]) == set(info_forecast.KNOWN_PLATFORMS)


# ── Offline heuristic path: valid forecast via info_forecast, clearly labelled ─

def test_offline_forecast_returns_valid_dict(monkeypatch):
    monkeypatch.setenv("NARVE_OFFLINE", "1")
    items = [
        {"platform": "x", "text": "candidate surges to a strong lead, gaining momentum"},
        {"platform": "reddit", "text": "ground game looks strong, ahead in polls"},
    ]
    out = nl.forecast_event("Will the candidate win?", items)

    # Valid forecast dict shape (coerced by info_forecast).
    assert 0.0 <= out["probability_yes"] <= 1.0
    assert 0.0 <= out["confidence"] <= 1.0
    assert out["n_items"] == 2
    assert set(out["source_spread"]) == set(info_forecast.KNOWN_PLATFORMS)

    # OBVIOUSLY labelled offline — never mistakable for the real model.
    assert out["political_lean"] == nl.OFFLINE_LEAN
    assert "OFFLINE HEURISTIC" in out["digest"]

    # Positive-leaning text -> probability above 0.5.
    assert out["probability_yes"] > 0.5


def test_offline_negative_text_below_half(monkeypatch):
    monkeypatch.setenv("NARVE_OFFLINE", "1")
    items = [
        {"platform": "news", "text": "candidate trailing badly, losing support amid scandal"},
        {"platform": "x", "text": "polls show a steep decline, underdog now"},
    ]
    out = nl.forecast_event("Will the candidate win?", items)
    assert out["probability_yes"] < 0.5
    assert out["political_lean"] == nl.OFFLINE_LEAN


def test_offline_neutral_text_is_half(monkeypatch):
    monkeypatch.setenv("NARVE_OFFLINE", "1")
    items = [{"platform": "news", "text": "the event is scheduled for tuesday"}]
    out = nl.forecast_event("Will it occur?", items)
    assert out["probability_yes"] == 0.5


def test_offline_deterministic(monkeypatch):
    monkeypatch.setenv("NARVE_OFFLINE", "1")
    items = [{"platform": "x", "text": "strong lead and gaining"},
             {"platform": "reddit", "text": "ahead, winning"}]
    a = nl.forecast_event("q?", items)
    b = nl.forecast_event("q?", items)
    assert a["probability_yes"] == b["probability_yes"]
    assert a["digest"] == b["digest"]


def test_offline_empty_items_still_raises(monkeypatch):
    """info_forecast's empty-input contract holds regardless of the llm."""
    monkeypatch.setenv("NARVE_OFFLINE", "1")
    with pytest.raises(ValueError):
        nl.forecast_event("q?", [])
