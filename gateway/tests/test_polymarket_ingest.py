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

from integrations.polymarket_ingest import build_market_record

def test_build_market_record_shape():
    market = {
        "id": 19, "slug": "kanye-divorce", "question": "Will they divorce?",
        "conditionId": "0xabc", "endDate": "2024-01-01T00:00:00Z",
        "outcomes": '["Yes","No"]', "outcomePrices": '["0.0000001","0.9999"]',
        "lastTradePrice": "0.45",
    }
    trades = [
        {"proxyWallet": "0xWALLET1", "outcome": "No", "side": "BUY", "size": 100, "price": 0.40, "timestamp": 1700000000},
        {"proxyWallet": "0xWALLET2", "outcome": "Yes", "side": "BUY", "size": 50, "price": 0.55, "timestamp": 1700000100},
    ]
    rec = build_market_record(market, trades)
    assert rec["market_id"] == "kanye-divorce"
    assert rec["resolved_outcome"] == 0          # "No" won
    assert rec["resolved_at"].startswith("2024-01-01")
    assert len(rec["price_timeline"]) >= 1
    handles = {f["source_handle"] for f in rec["forecasts"]}
    assert handles == {"0xWALLET1", "0xWALLET2"}
    for f in rec["forecasts"]:
        assert f["made_at"] < rec["resolved_at"]  # lookahead-safe

def test_build_market_record_none_when_ambiguous():
    market = {"id": 1, "slug": "x", "question": "q", "conditionId": "0x",
              "endDate": "2024-01-01T00:00:00Z",
              "outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]'}
    assert build_market_record(market, []) is None

def test_build_market_record_drops_lookahead_forecasts():
    # a forecast timestamped AFTER resolution must be dropped; if that leaves no
    # forecasts, the record is None
    market = {"id": 2, "slug": "y", "question": "q", "conditionId": "0x",
              "endDate": "2024-01-01T00:00:00Z",
              "outcomes": '["Yes","No"]', "outcomePrices": '["0.9999","0.0001"]',
              "lastTradePrice": "0.8"}
    future = [{"proxyWallet": "0xLATE", "outcome": "Yes", "side": "BUY",
               "size": 10, "price": 0.7, "timestamp": 1800000000}]  # year 2027, after 2024 resolution
    assert build_market_record(market, future) is None
