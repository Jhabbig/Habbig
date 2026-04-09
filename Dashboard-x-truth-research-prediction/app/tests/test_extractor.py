from app.processing.extractor import PredictionExtractor, fuzzy_match_score, match_to_market, infer_category

extractor = PredictionExtractor()

def test_explicit_percentage():
    r = extractor.extract("I think there is a 70% chance that Bitcoin will reach 100k by end of year, this is my strong conviction.")
    assert len(r) >= 1 and r[0].predicted_probability == 0.70

def test_percentage_id_say():
    r = extractor.extract("Based on current trends and analysis I'd say 85% that the senate bill passes before the summer recess.")
    assert len(r) >= 1 and r[0].predicted_probability == 0.85

def test_directional_will_win():
    r = extractor.extract("Trump will definitely win the Republican primary based on all the current polling data and momentum.")
    assert len(r) >= 1 and r[0].predicted_outcome == "Yes"

def test_negation_will_not():
    r = extractor.extract("There is absolutely no way the infrastructure bill will pass the senate this session, the votes aren't there.")
    assert len(r) >= 1 and r[0].predicted_outcome == "No"

def test_conditional_if_then():
    r = extractor.extract("If the Fed raises rates by another quarter point then inflation will come down significantly before Q4.")
    assert len(r) >= 1 and r[0].extraction_method == "conditional"

def test_bet_on_keyword():
    r = extractor.extract("I'm betting on Ethereum to outperform Bitcoin this quarter, the fundamentals are clearly stronger right now.")
    assert len(r) >= 1

def test_my_prediction_keyword():
    r = extractor.extract("My prediction is that the Lakers will win the championship this year, they have the best roster in the league.")
    assert len(r) >= 1

def test_rhetorical_question_discarded():
    assert extractor.extract("Will Trump ever learn to stop tweeting controversial things? I really wonder about it sometimes honestly.") == []

def test_past_tense_discarded():
    assert extractor.extract("Biden won the election last November in a close race, the results were certified by all fifty states quickly.") == []

def test_short_post_discarded():
    assert extractor.extract("BTC to 100k") == []

def test_empty_post_discarded():
    assert extractor.extract("") == []

def test_sale_context_discarded():
    assert extractor.extract("Amazing deal! Get 50% off all items in our store this weekend only, use promo code SAVE50 at checkout now.") == []

def test_malformed_no_crash():
    assert extractor.extract(None) == []

def test_fuzzy_match_strong():
    assert fuzzy_match_score("Trump will win the 2024 election", "Will Donald Trump win the 2024 Presidential Election?") > 0.50

def test_fuzzy_match_weak():
    assert fuzzy_match_score("Lakers win championship", "Will inflation exceed 5% in 2025?") == 0.0

def test_fuzzy_match_cross_category_rejected():
    assert fuzzy_match_score("Bulgaria hold parliamentary elections", "Will LeBron James be the next president") == 0.0

def test_fuzzy_match_min_overlap():
    assert fuzzy_match_score("bitcoin price", "bitcoin crash") == 0.0

def test_match_to_market_category_filter():
    markets = [
        {"market_question": "Will Trump win the 2026 midterm senate race?", "category": "politics"},
        {"market_question": "Will LeBron James win MVP this season in the NBA finals?", "category": "sports"},
    ]
    matched, score = match_to_market(
        "Trump looks strong going into the 2026 midterm senate race", markets, category="politics"
    )
    assert matched is not None
    assert matched["category"] == "politics"
    assert score >= 0.50

def test_match_to_market_strict_no_fallback():
    # If no markets exist in the requested category, we must return None rather
    # than silently matching against the wrong category (the original bug).
    markets = [
        {"market_question": "Will LeBron James be elected president of the United States?", "category": "sports"},
    ]
    matched, score = match_to_market(
        "Will Bulgaria hold early parliamentary elections in 2026", markets, category="politics"
    )
    assert matched is None
    assert score == 0.0

def test_match_to_market_pretokenized_fast_path():
    # When callers supply pre-computed _tokens, the matcher should use them
    # directly without re-tokenizing the market question.
    markets = [
        {
            "market_question": "Will Trump win the 2026 midterm senate race?",
            "category": "politics",
            "_tokens": {"trump", "win", "2026", "midterm", "senate", "race"},
        },
    ]
    matched, score = match_to_market(
        "Trump will win the 2026 midterm senate race", markets, category="politics"
    )
    assert matched is not None
    assert score >= 0.50

def test_infer_politics():
    assert infer_category("Trump will win the presidential election") == "politics"

def test_infer_crypto():
    assert infer_category("Bitcoin and Ethereum are going to surge to new highs") == "crypto"

def test_infer_other():
    assert infer_category("Something completely unrelated to any market") == "other"
