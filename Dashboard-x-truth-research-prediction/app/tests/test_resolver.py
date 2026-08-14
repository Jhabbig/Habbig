from app.markets.polymarket import PolymarketClient
from app.processing.resolver import MarketResolver

c = PolymarketClient()

def test_resolve_yes(): assert c.detect_resolution({"closed": True, "outcomePrices": ["0.9999844", "0.0000156"], "outcomes": ["Yes", "No"]}) == "Yes"
def test_resolve_no(): assert c.detect_resolution({"closed": True, "outcomePrices": ["0.00000016", "0.9999998"], "outcomes": ["Yes", "No"]}) == "No"
def test_resolve_voided(): assert c.detect_resolution({"closed": True, "outcomePrices": ["0", "0"], "outcomes": ["Yes", "No"]}) is None
def test_resolve_not_closed(): assert c.detect_resolution({"closed": False, "outcomePrices": ["0.52", "0.48"], "outcomes": ["Yes", "No"]}) is None
def test_resolve_multi(): assert c.detect_resolution({"closed": True, "outcomePrices": ["0", "0", "0", "0.9999999", "0"], "outcomes": ["A", "B", "C", "D", "E"]}) == "D"
def test_resolve_no_prices(): assert c.detect_resolution({"closed": True, "outcomePrices": [], "outcomes": []}) is None
def test_resolve_json_string(): assert c.detect_resolution({"closed": True, "outcomePrices": '["0.9999", "0.0001"]', "outcomes": '["Yes", "No"]'}) == "Yes"
def test_parse_prices_list(): assert c.parse_prices({"outcomePrices": ["0.52", "0.48"]}) == [0.52, 0.48]
def test_parse_prices_json(): assert c.parse_prices({"outcomePrices": '["0.52", "0.48"]'}) == [0.52, 0.48]
def test_parse_outcomes_list(): assert c.parse_outcomes({"outcomes": ["Yes", "No"]}) == ["Yes", "No"]
def test_parse_outcomes_json(): assert c.parse_outcomes({"outcomes": '["Yes", "No"]'}) == ["Yes", "No"]
def test_categorize_politics(): assert c.categorize_market("Will the president win the election?") == "politics"
def test_categorize_crypto(): assert c.categorize_market("Will Bitcoin reach $200k?") == "crypto"
def test_categorize_sports(): assert c.categorize_market("Will the Lakers win the championship finals?") == "sports"
def test_categorize_other(): assert c.categorize_market("Something completely unrelated") == "other"
def test_check_correct_yes(): assert MarketResolver._check_correct("Yes", "Yes") is True
def test_check_correct_no(): assert MarketResolver._check_correct("Yes", "No") is False
def test_check_correct_case(): assert MarketResolver._check_correct("yes", "Yes") is True


def _p(side, outcome="Yes"):
    from app.models import Prediction
    return Prediction(raw_post_id="x:1", category="other", predicted_outcome=outcome, bet_side=side)


def test_bet_won_yes_side_yes_outcome(): assert MarketResolver._bet_won(_p("YES"), "Yes") is True
def test_bet_won_yes_side_no_outcome(): assert MarketResolver._bet_won(_p("YES"), "No") is False
def test_bet_won_no_side_yes_outcome(): assert MarketResolver._bet_won(_p("NO"), "Yes") is False
def test_bet_won_no_side_no_outcome(): assert MarketResolver._bet_won(_p("NO"), "No") is True
def test_bet_won_named_outcome_match(): assert MarketResolver._bet_won(_p("YES", "Trump"), "Trump") is True
def test_bet_won_named_outcome_mismatch(): assert MarketResolver._bet_won(_p("YES", "Trump"), "Harris") is False
def test_bet_won_true_synonym(): assert MarketResolver._bet_won(_p("YES"), "true") is True
def test_bet_won_false_synonym(): assert MarketResolver._bet_won(_p("NO"), "false") is True
