from app.processing.extractor import PredictionExtractor, fuzzy_match_score, infer_category

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
    assert fuzzy_match_score("Trump will win the 2024 election", "Will Donald Trump win the 2024 Presidential Election?") > 0.35

def test_fuzzy_match_weak():
    assert fuzzy_match_score("Lakers win championship", "Will inflation exceed 5% in 2025?") < 0.35

def test_infer_politics():
    assert infer_category("Trump will win the presidential election") == "politics"

def test_infer_crypto():
    assert infer_category("Bitcoin and Ethereum are going to surge to new highs") == "crypto"

def test_infer_other():
    assert infer_category("Something completely unrelated to any market") == "other"
