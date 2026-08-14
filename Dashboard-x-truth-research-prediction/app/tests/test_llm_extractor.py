"""LLM extractor tests with a mocked Anthropic client.

We do NOT make real API calls — the tests stub out ``messages.parse`` so the
extractor's plumbing (cache read/write, fallback ordering, confidence filter)
gets exercised without any network or key requirement.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.processing import llm_extractor
from app.processing.extractor import PredictionExtractor


@pytest.fixture(autouse=True)
def _reset_llm_state():
    """Reset the cached client and re-enable extraction for each test."""
    llm_extractor.reset_client_for_tests()
    yield
    llm_extractor.reset_client_for_tests()


def _fake_response(predictions):
    """Build a SimpleNamespace shaped like ``client.messages.parse`` returns."""
    parsed = SimpleNamespace(predictions=[SimpleNamespace(**p) for p in predictions])
    return SimpleNamespace(parsed_output=parsed)


def test_is_available_false_without_api_key(monkeypatch):
    monkeypatch.setitem(llm_extractor.settings, "ANTHROPIC_API_KEY", "")
    assert llm_extractor.is_available() is False


def test_is_available_false_when_disabled(monkeypatch):
    monkeypatch.setitem(llm_extractor.settings, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setitem(llm_extractor.settings, "LLM_EXTRACTION_ENABLED", False)
    assert llm_extractor.is_available() is False


def test_is_available_true_when_key_set(monkeypatch):
    monkeypatch.setitem(llm_extractor.settings, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setitem(llm_extractor.settings, "LLM_EXTRACTION_ENABLED", True)
    assert llm_extractor.is_available() is True


@pytest.mark.asyncio
async def test_extract_returns_empty_when_unavailable(monkeypatch, async_engine):
    monkeypatch.setitem(llm_extractor.settings, "ANTHROPIC_API_KEY", "")
    import app.db as db_module
    db_module.engine = async_engine
    llm_extractor.engine = async_engine
    result = await llm_extractor.extract("Trump will win Pennsylvania this November.")
    assert result == []


@pytest.mark.asyncio
async def test_extract_parses_response_and_filters_low_confidence(monkeypatch, async_engine):
    monkeypatch.setitem(llm_extractor.settings, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setitem(llm_extractor.settings, "LLM_EXTRACTION_ENABLED", True)
    import app.db as db_module
    db_module.engine = async_engine
    llm_extractor.engine = async_engine

    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            parse=AsyncMock(return_value=_fake_response([
                {
                    "predicted_outcome": "Yes",
                    "predicted_probability": 0.75,
                    "category": "politics",
                    "raw_text": "Trump wins PA",
                    "confidence": 0.9,
                },
                {
                    # Low-confidence prediction — filtered out by the 0.5 floor
                    "predicted_outcome": "No",
                    "predicted_probability": None,
                    "category": "other",
                    "raw_text": "maybe a thing",
                    "confidence": 0.3,
                },
            ]))
        )
    )
    with patch.object(llm_extractor, "_get_client", return_value=fake_client):
        results = await llm_extractor.extract("Trump will win Pennsylvania with 75% probability this November.")

    assert len(results) == 1
    assert results[0].predicted_outcome == "Yes"
    assert abs(results[0].predicted_probability - 0.75) < 1e-9
    assert results[0].category == "politics"
    assert results[0].extraction_method == "llm"


@pytest.mark.asyncio
async def test_extract_caches_results_and_skips_second_call(monkeypatch, async_engine):
    monkeypatch.setitem(llm_extractor.settings, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setitem(llm_extractor.settings, "LLM_EXTRACTION_ENABLED", True)
    import app.db as db_module
    db_module.engine = async_engine
    llm_extractor.engine = async_engine

    parse_mock = AsyncMock(return_value=_fake_response([{
        "predicted_outcome": "Yes",
        "predicted_probability": 0.6,
        "category": "crypto",
        "raw_text": "BTC to 200k by EOY",
        "confidence": 0.85,
    }]))
    fake_client = SimpleNamespace(messages=SimpleNamespace(parse=parse_mock))

    content = "Bitcoin will hit 200k by end of year. About a 60% chance imo."
    with patch.object(llm_extractor, "_get_client", return_value=fake_client):
        first = await llm_extractor.extract(content)
        second = await llm_extractor.extract(content)

    assert parse_mock.await_count == 1  # second call was served from the DB cache
    assert len(first) == 1 and len(second) == 1
    assert first[0].extraction_method == "llm"
    assert second[0].extraction_method == "llm_cached"


@pytest.mark.asyncio
async def test_extract_returns_empty_on_api_failure(monkeypatch, async_engine):
    monkeypatch.setitem(llm_extractor.settings, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setitem(llm_extractor.settings, "LLM_EXTRACTION_ENABLED", True)
    import app.db as db_module
    db_module.engine = async_engine
    llm_extractor.engine = async_engine

    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            parse=AsyncMock(side_effect=RuntimeError("upstream 500"))
        )
    )
    with patch.object(llm_extractor, "_get_client", return_value=fake_client):
        result = await llm_extractor.extract("Eagles will win the Super Bowl this year, calling it now.")
    assert result == []


@pytest.mark.asyncio
async def test_extractor_async_uses_regex_first_skips_llm(monkeypatch, async_engine):
    """Regex hits should never invoke the LLM (cost + latency)."""
    monkeypatch.setitem(llm_extractor.settings, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setitem(llm_extractor.settings, "LLM_EXTRACTION_ENABLED", True)
    import app.db as db_module
    db_module.engine = async_engine
    llm_extractor.engine = async_engine

    parse_mock = AsyncMock()
    fake_client = SimpleNamespace(messages=SimpleNamespace(parse=parse_mock))
    with patch.object(llm_extractor, "_get_client", return_value=fake_client):
        results = await PredictionExtractor().extract_async(
            "I'd put it at about 80% chance Bitcoin breaks above one hundred thousand."
        )

    assert parse_mock.await_count == 0
    assert len(results) >= 1
    assert all(r.extraction_method == "percentage" for r in results)


@pytest.mark.asyncio
async def test_extractor_async_falls_through_to_llm(monkeypatch, async_engine):
    """Posts that fail every regex pattern should hit the LLM."""
    monkeypatch.setitem(llm_extractor.settings, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setitem(llm_extractor.settings, "LLM_EXTRACTION_ENABLED", True)
    import app.db as db_module
    db_module.engine = async_engine
    llm_extractor.engine = async_engine

    parse_mock = AsyncMock(return_value=_fake_response([{
        "predicted_outcome": "Yes",
        "predicted_probability": None,
        "category": "geopolitics",
        "raw_text": "ceasefire by spring",
        "confidence": 0.8,
    }]))
    fake_client = SimpleNamespace(messages=SimpleNamespace(parse=parse_mock))
    # A natural-language post the regex can't handle (no "X% chance", no
    # "will win", no "I think ... will"). The LLM should pick it up.
    with patch.object(llm_extractor, "_get_client", return_value=fake_client):
        results = await PredictionExtractor().extract_async(
            "Hard to read the diplomatic signals but my money is on a ceasefire being announced before spring rolls around."
        )

    assert parse_mock.await_count == 1
    assert len(results) == 1
    assert results[0].extraction_method == "llm"
    assert results[0].category == "geopolitics"


@pytest.mark.asyncio
async def test_extractor_async_falls_through_to_keyword_when_llm_unavailable(async_engine, monkeypatch):
    """If no API key is set, the keyword fallback still runs (legacy behavior)."""
    monkeypatch.setitem(llm_extractor.settings, "ANTHROPIC_API_KEY", "")
    import app.db as db_module
    db_module.engine = async_engine
    llm_extractor.engine = async_engine

    results = await PredictionExtractor().extract_async(
        "Honestly my prediction is the markets are gonna do something interesting this quarter."
    )
    # "predict" appears in the prediction_keywords list -> keyword fallback fires
    assert len(results) == 1
    assert results[0].extraction_method == "keyword"
