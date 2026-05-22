"""Multi-outcome matcher tests.

The key invariant: a prediction about Trump must never match the Harris market
in a multi-outcome event, even though their question texts share ~90% of their
tokens (a Jaccard score of ~0.85 would otherwise win out over the right
candidate at ~0.93).
"""
from __future__ import annotations

from app.processing.extractor import _outcome_appears_in, _tokenize, match_to_market


def test_outcome_appears_single_word():
    assert _outcome_appears_in(_tokenize("Trump will win 2028"), "Trump") is True


def test_outcome_does_not_appear():
    assert _outcome_appears_in(_tokenize("Harris will win 2028"), "Trump") is False


def test_outcome_appears_multi_word_requires_all_tokens():
    """Multi-word outcomes ('RFK Jr.') need every token present."""
    assert _outcome_appears_in(_tokenize("RFK Jr will run independently"), "RFK Jr") is True
    # Just "Jr" isn't enough — would otherwise spuriously match "Jr." against
    # any post mentioning "junior", "Senator Jr", etc.
    assert _outcome_appears_in(_tokenize("Trump junior says he'll join"), "RFK Jr") is False


def test_outcome_empty_passes_through():
    # Binary markets have no outcome_name -> never filtered.
    assert _outcome_appears_in(_tokenize("anything"), "") is True


def _mkt(slug, question, category="politics", outcome_name=None, event_title=None):
    return {
        "market_slug": slug,
        "market_question": question,
        "category": category,
        "outcome_name": outcome_name,
        "event_title": event_title,
    }


def test_match_picks_correct_candidate_in_multi_outcome_event():
    """Prediction about Trump must match the Trump market, not the Harris one,
    even though both share 'Will X win the 2028 Presidential Election?'."""
    markets = [
        _mkt("trump-2028", "Will Trump win the 2028 Presidential Election?",
             outcome_name="Trump", event_title="2028 Presidential Election Winner"),
        _mkt("harris-2028", "Will Harris win the 2028 Presidential Election?",
             outcome_name="Harris", event_title="2028 Presidential Election Winner"),
        _mkt("rfk-2028", "Will RFK Jr win the 2028 Presidential Election?",
             outcome_name="RFK Jr", event_title="2028 Presidential Election Winner"),
    ]
    matched, score = match_to_market(
        "I think Trump will win the 2028 presidential election handily",
        markets, threshold=0.3, category="politics",
    )
    assert matched is not None
    assert matched["market_slug"] == "trump-2028"
    assert score > 0.3


def test_match_filters_out_non_named_outcomes():
    """If no candidate is named in the prediction, every multi-outcome market
    is filtered out and the matcher returns None."""
    markets = [
        _mkt("trump-2028", "Will Trump win the 2028 Presidential Election?", outcome_name="Trump"),
        _mkt("harris-2028", "Will Harris win the 2028 Presidential Election?", outcome_name="Harris"),
    ]
    matched, _ = match_to_market(
        "The 2028 presidential election is going to be a wild ride for everyone",
        markets, threshold=0.3, category="politics",
    )
    assert matched is None


def test_match_still_works_for_binary_markets():
    """Binary markets (outcome_name=None) match like the legacy behaviour."""
    markets = [
        _mkt("fed-march", "Will the Fed cut rates at the March 2026 meeting?", outcome_name=None),
    ]
    matched, score = match_to_market(
        "I bet the Fed cuts rates at the March meeting this year",
        markets, threshold=0.2, category="politics",
    )
    assert matched is not None
    assert matched["market_slug"] == "fed-march"


def test_match_handles_mixed_binary_and_multi_outcome():
    """When the same category has both binary and multi-outcome markets, the
    binary still wins if it's the better Jaccard match and no candidate is named."""
    markets = [
        _mkt("trump-2028", "Will Trump win the 2028 Presidential Election?",
             outcome_name="Trump", event_title="2028 election"),
        _mkt("recession-2026", "Will the US enter a recession in 2026?", outcome_name=None),
    ]
    matched, _ = match_to_market(
        "The US is going to enter a recession in 2026 for sure",
        markets, threshold=0.3, category="politics",
    )
    assert matched is not None
    assert matched["market_slug"] == "recession-2026"


def test_polymarket_parse_event_info_extracts_fields():
    from app.markets.polymarket import PolymarketClient
    client = PolymarketClient()
    event_slug, event_title, outcome_name = client.parse_event_info({
        "groupItemTitle": "Trump",
        "events": [{"slug": "election-2028", "title": "2028 Presidential Election Winner"}],
    })
    assert event_slug == "election-2028"
    assert event_title == "2028 Presidential Election Winner"
    assert outcome_name == "Trump"


def test_polymarket_parse_event_info_handles_binary():
    from app.markets.polymarket import PolymarketClient
    client = PolymarketClient()
    event_slug, event_title, outcome_name = client.parse_event_info({
        "groupItemTitle": "",
        "events": [],
    })
    assert event_slug is None and event_title is None and outcome_name is None
