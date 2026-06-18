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
