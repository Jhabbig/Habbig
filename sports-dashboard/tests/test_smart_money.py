"""Tests for the smart-money aggregation that overlays top-trader
positions onto sports comparisons (T2.5)."""
import sports_dashboard as sd


def _comp(home="Lakers", away="Warriors", positions=None, **extras):
    """Build a comparison dict with attached top-trader positions."""
    base = {
        "home_team": home, "away_team": away,
        "commence_time": "2026-01-15T20:00:00Z",
        "condition_id": "cond_abc",
        "poly_slug": "lakers-vs-warriors",
        "poly_question": "Will Lakers beat Warriors?",
        "trade_poly_url": "https://polymarket.com/market/lakers-vs-warriors",
        "has_signal": False, "max_divergence": 0,
        "top_trader_positions": positions or [],
        "outcomes": [],
    }
    base.update(extras)
    return base


def _pos(wallet, outcome, net_usd, net_size=None, avg_price=0.4, rank=10,
         name="", pseudonym="", last_ts=1737000000):
    """Build a top-trader position row."""
    return {
        "wallet": wallet, "outcome": outcome,
        "net_usd": net_usd,
        "net_size": net_size if net_size is not None else abs(net_usd) / avg_price,
        "avg_price": avg_price,
        "rank": rank,
        "name": name, "pseudonym": pseudonym,
        "last_traded_ts": last_ts,
        "last_side": "BUY" if net_usd > 0 else "SELL",
    }


# ── _smart_money_for_comparisons ────────────────────────────────────────────

def test_empty_returns_empty():
    assert sd._smart_money_for_comparisons([]) == []


def test_comparison_without_positions_is_skipped():
    rows = sd._smart_money_for_comparisons([_comp(positions=[])])
    assert rows == []


def test_single_position_one_side():
    comp = _comp(positions=[
        _pos("0xWHALE1", "Lakers", net_usd=10000, avg_price=0.45),
    ])
    rows = sd._smart_money_for_comparisons([comp])
    assert len(rows) == 1
    r = rows[0]
    assert r["total_whales"] == 1
    assert r["total_usd"] == 10000.0
    assert len(r["sides"]) == 1
    assert r["sides"][0]["outcome"] == "Lakers"
    assert r["sides"][0]["n_whales"] == 1
    assert r["sides"][0]["net_usd"] == 10000.0


def test_dust_positions_are_dropped():
    """Sides with |net_usd| < $50 are noise — drop them so they don't
    clutter the UI."""
    comp = _comp(positions=[
        _pos("0xDUST", "Lakers", net_usd=10),
        _pos("0xREAL", "Warriors", net_usd=5000),
    ])
    rows = sd._smart_money_for_comparisons([comp])
    assert len(rows) == 1
    sides = {s["outcome"]: s for s in rows[0]["sides"]}
    assert "Warriors" in sides
    assert "Lakers" not in sides


def test_market_with_only_dust_is_skipped_entirely():
    """If every side gets filtered, drop the comparison entirely."""
    comp = _comp(positions=[
        _pos("0xa", "Lakers", net_usd=5),
        _pos("0xb", "Warriors", net_usd=-10),
    ])
    assert sd._smart_money_for_comparisons([comp]) == []


def test_aggregates_per_side():
    """Multiple wallets on the same side sum net_usd; counts wallets;
    weighted-average entry price."""
    comp = _comp(positions=[
        _pos("0xa", "Lakers", net_usd=10000, net_size=20000, avg_price=0.50),
        _pos("0xb", "Lakers", net_usd=4000,  net_size=10000, avg_price=0.40),
    ])
    rows = sd._smart_money_for_comparisons([comp])
    side = rows[0]["sides"][0]
    assert side["n_whales"] == 2
    assert side["net_usd"] == 14000.0
    # Weighted avg by abs(net_size): (0.50*20000 + 0.40*10000) / 30000 = 14000/30000 ≈ 0.4667
    assert abs(side["avg_entry_price"] - 0.4667) < 0.001


def test_top_wallets_capped_at_five():
    positions = [
        _pos(f"0x{i:02x}", "Lakers", net_usd=1000 * (10 - i))
        for i in range(8)  # 8 wallets, descending net_usd
    ]
    comp = _comp(positions=positions)
    side = sd._smart_money_for_comparisons([comp])[0]["sides"][0]
    assert len(side["top_wallets"]) == 5
    # Highest exposure should be first
    assert side["top_wallets"][0]["net_usd"] > side["top_wallets"][-1]["net_usd"]


def test_sides_sorted_by_abs_exposure():
    comp = _comp(positions=[
        _pos("0xsmall", "Lakers", net_usd=200),
        _pos("0xbig",   "Warriors", net_usd=50000),
    ])
    sides = sd._smart_money_for_comparisons([comp])[0]["sides"]
    assert sides[0]["outcome"] == "Warriors"
    assert sides[1]["outcome"] == "Lakers"


def test_markets_sorted_by_total_usd_desc():
    big = _comp(home="A", away="B", positions=[_pos("0x1", "A", 100000)])
    small = _comp(home="C", away="D", positions=[_pos("0x2", "C", 500)])
    rows = sd._smart_money_for_comparisons([small, big])
    assert rows[0]["home_team"] == "A"
    assert rows[1]["home_team"] == "C"


def test_negative_exposure_preserved():
    """A whale selling (or shorting) carries negative net_usd — that's
    still meaningful and should pass the dust filter."""
    comp = _comp(positions=[_pos("0xbear", "Lakers", net_usd=-8000)])
    side = sd._smart_money_for_comparisons([comp])[0]["sides"][0]
    assert side["net_usd"] == -8000.0
    # total_usd uses abs() so it's the magnitude of conviction either way
    assert sd._smart_money_for_comparisons([comp])[0]["total_usd"] == 8000.0


def test_most_recent_trade_timestamp_kept():
    comp = _comp(positions=[
        _pos("0xa", "Lakers", net_usd=5000, last_ts=1737000000),
        _pos("0xb", "Lakers", net_usd=2000, last_ts=1737001000),  # newer
        _pos("0xc", "Lakers", net_usd=1000, last_ts=1736999000),  # older
    ])
    side = sd._smart_money_for_comparisons([comp])[0]["sides"][0]
    assert side["last_trade_ts"] == 1737001000


def test_wallet_display_includes_pseudonym_and_rank():
    """Top-wallet entries pass through pseudonym, name, and rank so the
    UI can render a human-readable identity."""
    comp = _comp(positions=[
        _pos("0xa", "Lakers", net_usd=5000, name="Theo", pseudonym="theo.eth", rank=3),
    ])
    w = sd._smart_money_for_comparisons([comp])[0]["sides"][0]["top_wallets"][0]
    assert w["pseudonym"] == "theo.eth"
    assert w["name"] == "Theo"
    assert w["rank"] == 3
