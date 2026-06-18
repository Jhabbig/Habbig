from integrations.polymarket_ingest import decisive_outcome

def test_decisive_outcome_clean_yes():
    # outcomePrices ~[1,0], outcomes ["Yes","No"] -> YES won (index 0)
    assert decisive_outcome(["0.999999", "0.000001"], ["Yes", "No"]) == (1, "Yes")

def test_decisive_outcome_clean_no():
    assert decisive_outcome(["0.0000001", "0.9999"], ["Yes", "No"]) == (0, "No")

def test_decisive_outcome_rejects_ambiguous():
    assert decisive_outcome(["0.52", "0.48"], ["Yes", "No"]) is None
    assert decisive_outcome(["0", "0"], ["Yes", "No"]) is None
    assert decisive_outcome(None, ["Yes", "No"]) is None

from integrations.polymarket_ingest import wallet_prediction

def _trade(outcome, side, size, price, ts):
    return {"outcome": outcome, "side": side, "size": size, "price": price, "timestamp": ts}

def test_buy_yes_predicts_yes():
    p = wallet_prediction([_trade("Yes", "BUY", 100, 0.60, 1000)])
    assert p["direction"] == "YES"
    assert abs(p["predicted_probability"] - 0.60) < 1e-6
    assert p["made_at_ts"] == 1000

def test_sell_yes_is_a_bet_on_no():
    # SELL Yes @0.60 == bet on NO (integrity trap #3)
    p = wallet_prediction([_trade("Yes", "SELL", 100, 0.60, 2000)])
    assert p["direction"] == "NO"

def test_buy_no_predicts_no_with_yes_implied_price():
    # BUY No @0.30 -> YES-implied price 0.70, net YES exposure negative -> NO
    p = wallet_prediction([_trade("No", "BUY", 100, 0.30, 1500)])
    assert p["direction"] == "NO"

def test_net_zero_is_no_prediction():
    assert wallet_prediction([
        _trade("Yes", "BUY", 100, 0.50, 1),
        _trade("Yes", "SELL", 100, 0.50, 2),
    ]) is None

def test_earliest_timestamp_on_net_side():
    p = wallet_prediction([
        _trade("Yes", "BUY", 100, 0.55, 3000),
        _trade("Yes", "BUY", 100, 0.65, 1000),
    ])
    assert p["made_at_ts"] == 1000
    assert abs(p["predicted_probability"] - 0.60) < 1e-6  # size-weighted avg
