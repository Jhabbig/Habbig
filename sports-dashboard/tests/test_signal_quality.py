"""Tests for signal-quality flags: vig-adjustment, sharp consensus,
liquidity gate, stale-data gate, and trade-URL builder."""
import sports_dashboard as sd


def _make_event(consensus: dict, books: dict, sharp_outcome_prob: float) -> dict:
    """Build a minimal event with controlled bookmaker structure."""
    return {
        "consensus_probs": consensus,
        "bookmakers": books,
        "sharp_outcomes": {"Lakers": {"implied_prob": sharp_outcome_prob}},
        "sharp_book": "pinnacle",
        "num_bookmakers": len(books),
    }


def _make_pm(volume=5000, spread=0.02, last_trade=0.5, day_change=0.01, week_change=0.05):
    return {
        "volume": volume, "spread": spread,
        "last_trade_price": last_trade,
        "one_day_change": day_change,
        "one_week_change": week_change,
    }


# ── Vig adjustment ──────────────────────────────────────────────────────────

def test_vig_adjustment_reduces_apparent_divergence():
    """A 6pp raw divergence shrinks once we de-vig the sharp book.

    Pinnacle 56/47 sums to 103 (3% vig). De-vigged Lakers prob =
    56 / 103 * 100 ≈ 54.4. Against poly 50, the de-vigged divergence
    is 4.4pp, vs the raw 6pp.
    """
    event = _make_event(
        consensus={"Lakers": 56.0, "Warriors": 47.0},
        books={"pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 56.0}}}},
        sharp_outcome_prob=56.0,
    )
    q = sd._signal_quality(event, "Lakers", odds_prob=56.0, poly_prob=50.0,
                            poly_market=_make_pm())
    assert q["divergence_raw"] == 6.0
    assert 4.0 < q["divergence_devigged"] < 4.7
    assert q["vig_pct"] == 3.0


def test_no_vig_means_raw_equals_devigged():
    """Sum of consensus = 100 (no vig) => devigged divergence == raw."""
    event = _make_event(
        consensus={"Lakers": 50.0, "Warriors": 50.0},
        books={"pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 50.0}}}},
        sharp_outcome_prob=50.0,
    )
    q = sd._signal_quality(event, "Lakers", 50.0, 44.0, _make_pm())
    assert q["divergence_raw"] == 6.0
    assert q["divergence_devigged"] == 6.0
    assert q["vig_pct"] == 0.0


# ── Sharp consensus ─────────────────────────────────────────────────────────

def test_sharp_consensus_passes_with_one_sharp():
    event = _make_event(
        consensus={"Lakers": 55.0, "Warriors": 45.0},
        books={"pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 55.0}}}},
        sharp_outcome_prob=55.0,
    )
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0, _make_pm())
    assert q["sharp_consensus_ok"] is True
    assert "pinnacle" in q["sharp_books_present"]


def test_sharp_consensus_rejects_when_sharps_disagree():
    event = _make_event(
        consensus={"Lakers": 55.0, "Warriors": 45.0},
        books={
            "pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 53.0}}},
            "circa_h2h":    {"outcomes": {"Lakers": {"implied_prob": 60.0}}},
        },
        sharp_outcome_prob=53.0,
    )
    q = sd._signal_quality(event, "Lakers", 53.0, 50.0, _make_pm())
    # Pinnacle 53, Circa 60: spread 7pp > tolerance 2pp → reject.
    assert q["sharp_consensus_ok"] is False


def test_sharp_consensus_passes_when_sharps_agree_within_tolerance():
    event = _make_event(
        consensus={"Lakers": 55.0, "Warriors": 45.0},
        books={
            "pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 55.0}}},
            "circa_h2h":    {"outcomes": {"Lakers": {"implied_prob": 56.5}}},
        },
        sharp_outcome_prob=55.0,
    )
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0, _make_pm())
    # Sharps within 2pp → pass.
    assert q["sharp_consensus_ok"] is True


def test_no_sharp_books_fails():
    event = _make_event(
        consensus={"Lakers": 55.0},
        books={"draftkings_h2h": {"outcomes": {"Lakers": {"implied_prob": 55.0}}}},
        sharp_outcome_prob=55.0,
    )
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0, _make_pm())
    assert q["sharp_consensus_ok"] is False
    assert q["sharp_books_present"] == []


# ── Liquidity gate ──────────────────────────────────────────────────────────

def test_liquidity_gate_rejects_low_volume():
    event = _make_event(
        consensus={"Lakers": 55.0},
        books={"pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 55.0}}}},
        sharp_outcome_prob=55.0,
    )
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0,
                            _make_pm(volume=500))  # < $1k
    assert q["liquidity_ok"] is False


def test_liquidity_gate_rejects_wide_spread():
    event = _make_event(
        consensus={"Lakers": 55.0},
        books={"pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 55.0}}}},
        sharp_outcome_prob=55.0,
    )
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0,
                            _make_pm(spread=0.10))  # 10pp spread
    assert q["liquidity_ok"] is False


def test_liquidity_gate_passes_with_volume_and_spread():
    event = _make_event(
        consensus={"Lakers": 55.0},
        books={"pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 55.0}}}},
        sharp_outcome_prob=55.0,
    )
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0,
                            _make_pm(volume=5000, spread=0.02))
    assert q["liquidity_ok"] is True


# ── Stale-data gate ─────────────────────────────────────────────────────────

def test_stale_when_never_traded():
    event = _make_event(
        consensus={"Lakers": 55.0},
        books={"pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 55.0}}}},
        sharp_outcome_prob=55.0,
    )
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0,
                            _make_pm(last_trade=0.0))
    assert q["not_stale"] is False


def test_stale_when_completely_flat_for_a_week():
    event = _make_event(
        consensus={"Lakers": 55.0},
        books={"pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 55.0}}}},
        sharp_outcome_prob=55.0,
    )
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0,
                            _make_pm(day_change=0.0, week_change=0.0))
    assert q["not_stale"] is False


def test_not_stale_with_recent_movement():
    event = _make_event(
        consensus={"Lakers": 55.0},
        books={"pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 55.0}}}},
        sharp_outcome_prob=55.0,
    )
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0,
                            _make_pm(day_change=0.02, week_change=0.05))
    assert q["not_stale"] is True


# ── Combined gate ───────────────────────────────────────────────────────────

def test_passes_all_gates_requires_each_individually():
    """passes_all_gates should be the AND of the three quality flags."""
    event = _make_event(
        consensus={"Lakers": 55.0},
        books={"pinnacle_h2h": {"outcomes": {"Lakers": {"implied_prob": 55.0}}}},
        sharp_outcome_prob=55.0,
    )
    # Healthy market — all gates pass
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0, _make_pm())
    assert q["passes_all_gates"] is True
    # Drop liquidity — gate fails
    q = sd._signal_quality(event, "Lakers", 55.0, 50.0, _make_pm(volume=100))
    assert q["passes_all_gates"] is False


# ── Trade URL builder ───────────────────────────────────────────────────────

def test_build_trade_urls_polymarket():
    urls = sd._build_trade_urls(poly_slug="lakers-vs-warriors-2026-01-15")
    assert urls["trade_poly_url"] == "https://polymarket.com/market/lakers-vs-warriors-2026-01-15"
    assert urls["trade_kalshi_url"] is None


def test_build_trade_urls_kalshi_event():
    urls = sd._build_trade_urls(poly_slug=None, kalshi_event_ticker="KXNBAGAME-26JAN15LAKGSW")
    assert urls["trade_kalshi_url"] == "https://kalshi.com/events/kxnbagame-26jan15lakgsw"


def test_build_trade_urls_kalshi_specific_market():
    """Specific market ticker takes precedence over the event ticker."""
    urls = sd._build_trade_urls(
        poly_slug=None,
        kalshi_event_ticker="KXNBAGAME-26JAN15LAKGSW",
        kalshi_market_ticker="KXNBAGAME-26JAN15LAKGSW-GSW",
    )
    assert urls["trade_kalshi_url"] == "https://kalshi.com/markets/kxnbagame-26jan15lakgsw-gsw"


def test_build_trade_urls_both_none():
    urls = sd._build_trade_urls(poly_slug=None)
    assert urls == {"trade_poly_url": None, "trade_kalshi_url": None}
