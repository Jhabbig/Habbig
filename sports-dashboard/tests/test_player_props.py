"""Tests for the player-prop pipeline: name normalization, Odds API
parsing, Kalshi extraction, and cross-venue matching.

The fetcher functions hit network so they aren't unit-tested here —
the pure-function parsers and the matcher are."""
import sports_dashboard as sd


# ── normalize_player_name ───────────────────────────────────────────────────

def test_normalize_simple():
    assert sd.normalize_player_name("LeBron James") == "lebron james"


def test_normalize_strips_jr_suffix():
    assert sd.normalize_player_name("Marvin Harrison Jr.") == "marvin harrison"


def test_normalize_strips_roman_numerals():
    assert sd.normalize_player_name("Robert Griffin III") == "robert griffin"


def test_normalize_collapses_whitespace():
    assert sd.normalize_player_name("  LeBron   James  ") == "lebron james"


def test_normalize_resolves_nicknames():
    assert sd.normalize_player_name("KD") == "kevin durant"
    assert sd.normalize_player_name("Steph") == "stephen curry"
    assert sd.normalize_player_name("Giannis") == "giannis antetokounmpo"


def test_normalize_keeps_apostrophe():
    """Names like Ja'Marr Chase should keep the apostrophe."""
    assert sd.normalize_player_name("Ja'Marr Chase") == "ja'marr chase"


def test_normalize_empty():
    assert sd.normalize_player_name("") == ""
    assert sd.normalize_player_name(None) == ""


# ── _kalshi_series_to_market ────────────────────────────────────────────────

def test_kalshi_market_mapping():
    assert sd._kalshi_series_to_market("KXNBAPTS") == "player_points"
    assert sd._kalshi_series_to_market("KXNBA3PT") == "player_threes"
    assert sd._kalshi_series_to_market("KXNBAAST") == "player_assists"
    assert sd._kalshi_series_to_market("KXNBAREB") == "player_rebounds"


def test_kalshi_market_unknown_returns_none():
    assert sd._kalshi_series_to_market("KXNBAGAME") is None
    assert sd._kalshi_series_to_market("KXNFLGAME") is None


# ── _extract_kalshi_prop_line ───────────────────────────────────────────────

def test_extract_line_basic():
    assert sd._extract_kalshi_prop_line("KXNBAPTS-26JAN15LBJ-T26.5") == 26.5


def test_extract_line_integer():
    assert sd._extract_kalshi_prop_line("KXNBAPTS-26JAN15LBJ-T27") == 27.0


def test_extract_line_no_t_prefix():
    assert sd._extract_kalshi_prop_line("KXNBAPTS-26JAN15LBJ-25.5") == 25.5


def test_extract_line_missing():
    assert sd._extract_kalshi_prop_line("") is None
    assert sd._extract_kalshi_prop_line("KXNBAPTS") is None


# ── parse_player_props (Odds API) ───────────────────────────────────────────

def test_parse_player_props_pivots_per_player_line():
    """Two bookmakers, one player, one line → one row with two book entries."""
    raw = [{
        "id": "evt1",
        "home_team": "Lakers", "away_team": "Warriors",
        "commence_time": "2026-01-15T20:00:00Z",
        "bookmakers": [
            {"key": "draftkings", "title": "DraftKings", "markets": [
                {"key": "player_points", "outcomes": [
                    {"name": "Over", "description": "LeBron James", "point": 25.5, "price": 1.91},
                    {"name": "Under", "description": "LeBron James", "point": 25.5, "price": 1.91},
                ]},
            ]},
            {"key": "fanduel", "title": "FanDuel", "markets": [
                {"key": "player_points", "outcomes": [
                    {"name": "Over", "description": "LeBron James", "point": 25.5, "price": 1.95},
                    {"name": "Under", "description": "LeBron James", "point": 25.5, "price": 1.87},
                ]},
            ]},
        ],
    }]
    rows = sd.parse_player_props(raw)
    assert len(rows) == 1
    r = rows[0]
    assert r["player"] == "LeBron James"
    assert r["player_norm"] == "lebron james"
    assert r["market"] == "player_points"
    assert r["line"] == 25.5
    assert "draftkings" in r["books"]
    assert "fanduel" in r["books"]
    # DK: over @ 1.91 = ~52.4%
    assert 52 < r["books"]["draftkings"]["over_prob"] < 53


def test_parse_player_props_computes_vig_and_devig():
    raw = [{
        "id": "e", "home_team": "A", "away_team": "B", "commence_time": "",
        "bookmakers": [{"key": "dk", "title": "DK", "markets": [
            {"key": "player_points", "outcomes": [
                # 100/1.91 + 100/1.91 = 52.36 + 52.36 = 104.7 → 4.7% vig
                {"name": "Over", "description": "Player X", "point": 20.5, "price": 1.91},
                {"name": "Under", "description": "Player X", "point": 20.5, "price": 1.91},
            ]},
        ]}],
    }]
    rows = sd.parse_player_props(raw)
    assert len(rows) == 1
    assert abs(rows[0]["vig_pct"] - 4.71) < 0.1
    # Devig = 52.36 / 104.7 = ~50%
    assert 49 < rows[0]["consensus_over_devigged"] < 51


def test_parse_player_props_separates_lines():
    """Same player but two different lines → two rows."""
    raw = [{
        "id": "e", "home_team": "", "away_team": "", "commence_time": "",
        "bookmakers": [{"key": "dk", "title": "DK", "markets": [
            {"key": "player_points", "outcomes": [
                {"name": "Over", "description": "LeBron James", "point": 25.5, "price": 1.91},
                {"name": "Under", "description": "LeBron James", "point": 25.5, "price": 1.91},
                {"name": "Over", "description": "LeBron James", "point": 27.5, "price": 2.50},
                {"name": "Under", "description": "LeBron James", "point": 27.5, "price": 1.55},
            ]},
        ]}],
    }]
    rows = sd.parse_player_props(raw)
    assert len(rows) == 2
    lines = sorted(r["line"] for r in rows)
    assert lines == [25.5, 27.5]


def test_parse_player_props_ignores_non_prop_markets():
    raw = [{
        "id": "e", "home_team": "", "away_team": "", "commence_time": "",
        "bookmakers": [{"key": "dk", "title": "DK", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Team A", "price": 1.91},
                {"name": "Team B", "price": 1.91},
            ]},
        ]}],
    }]
    assert sd.parse_player_props(raw) == []


# ── parse_kalshi_player_props ───────────────────────────────────────────────

def test_parse_kalshi_player_props():
    parsed_kalshi = [{
        "event_ticker": "KXNBAPTS-26JAN15LBJ",
        "title": "LeBron James points - Jan 15",
        "market_type": "props",
        "teams": {
            "LeBron James": {
                "ticker": "KXNBAPTS-26JAN15LBJ-T27",
                "implied_prob": 50.0, "yes_bid": 0.49, "yes_ask": 0.51,
                "volume": 5000,
            }
        },
        "total_volume": 5000,
    }]
    out = sd.parse_kalshi_player_props(parsed_kalshi)
    assert len(out) == 1
    p = out[0]
    assert p["player_norm"] == "lebron james"
    assert p["market"] == "player_points"
    assert p["line_kalshi"] == 27.0
    # T27 means "score >= 27" = book over 26.5
    assert p["line_book_equivalent"] == 26.5


def test_parse_kalshi_player_props_ignores_non_props():
    """Events tagged as 'game' or 'futures' should not appear in the prop feed."""
    parsed_kalshi = [
        {"event_ticker": "KXNBAGAME-...", "market_type": "game", "teams": {}, "title": ""},
        {"event_ticker": "KXNBAMVP-...", "market_type": "futures", "teams": {}, "title": ""},
    ]
    assert sd.parse_kalshi_player_props(parsed_kalshi) == []


# ── _extract_poly_prop_info ─────────────────────────────────────────────────

def test_extract_poly_prop_classic_question():
    pm = {
        "market_question": "Will LeBron James score 30+ points?",
        "outcomes": {"Yes": {"implied_prob": 35.0}, "No": {"implied_prob": 65.0}},
    }
    info = sd._extract_poly_prop_info(pm)
    assert info is not None
    assert info["player_norm"] == "lebron james"
    assert info["market"] == "player_points"
    # "30+" -> ">= 30" -> book over 29.5
    assert info["line"] == 29.5
    assert info["yes_prob"] == 35.0


def test_extract_poly_prop_non_prop_returns_none():
    pm = {"market_question": "Will Lakers beat Warriors?", "outcomes": {"Yes": {"implied_prob": 50.0}}}
    assert sd._extract_poly_prop_info(pm) is None


def test_extract_poly_prop_assists():
    pm = {
        "market_question": "Will Chris Paul record 10+ assists?",
        "outcomes": {"Yes": {"implied_prob": 40.0}},
    }
    info = sd._extract_poly_prop_info(pm)
    assert info is not None
    assert info["market"] == "player_assists"
    assert info["line"] == 9.5


# ── match_player_props_cross_venue ──────────────────────────────────────────

def _book_prop(player="LeBron James", market="player_points", line=25.5,
               consensus_devig=52.0):
    return {
        "player": player,
        "player_norm": sd.normalize_player_name(player),
        "market": market,
        "line": line,
        "event": "Lakers @ Warriors",
        "commence_time": "",
        "consensus_over_pp": consensus_devig + 2.5,
        "consensus_over_devigged": consensus_devig,
        "consensus_under_pp": (100 - consensus_devig) + 2.5,
        "vig_pct": 5.0,
        "books": {
            "draftkings": {"title": "DK", "over_prob": consensus_devig + 1, "under_prob": 100 - consensus_devig + 4},
        },
    }


def _kalshi_prop(player="LeBron James", market="player_points",
                 line_book_equiv=25.5, yes_prob=45.0):
    return {
        "player": player,
        "player_norm": sd.normalize_player_name(player),
        "market": market,
        "line_kalshi": line_book_equiv + 0.5,
        "line_book_equivalent": line_book_equiv,
        "yes_prob": yes_prob,
        "yes_bid": 0.44, "yes_ask": 0.46,
        "volume": 5000,
        "ticker": "KXNBAPTS-X-T26",
        "event_ticker": "KXNBAPTS-X",
        "event_title": "LeBron James points",
    }


def test_match_book_to_kalshi_when_lines_align():
    """Book over 25.5 = Kalshi T26 (score >= 26). Should match and divergence
    should be (book_devig - kalshi_yes_prob)."""
    rows = sd.match_player_props_cross_venue(
        [_book_prop(consensus_devig=52.0)],
        [_kalshi_prop(yes_prob=45.0)],
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["kalshi"] is not None
    assert r["divergences"]["kalshi"] == 7.0  # 52 - 45
    # 7pp >= threshold (5) → signal
    assert r["is_signal"] is True


def test_match_no_kalshi_when_lines_differ():
    rows = sd.match_player_props_cross_venue(
        [_book_prop(line=25.5)],
        [_kalshi_prop(line_book_equiv=27.5)],
    )
    assert len(rows) == 1
    assert rows[0]["kalshi"] is None


def test_match_no_signal_when_divergence_small():
    """Divergence below threshold (5pp) -> not a signal."""
    rows = sd.match_player_props_cross_venue(
        [_book_prop(consensus_devig=50.0)],
        [_kalshi_prop(yes_prob=48.0)],  # 2pp difference
    )
    assert rows[0]["is_signal"] is False


def test_match_picks_higher_volume_kalshi_on_duplicate():
    """When two Kalshi rows share the same (player, market, line), keep the
    one with more volume."""
    rows = sd.match_player_props_cross_venue(
        [_book_prop()],
        [
            {**_kalshi_prop(), "volume": 100, "ticker": "low-vol"},
            {**_kalshi_prop(), "volume": 50000, "ticker": "high-vol"},
        ],
    )
    assert rows[0]["kalshi"]["ticker"] == "high-vol"


def test_match_attaches_polymarket_when_question_extractable():
    pm = {
        "slug": "lebron-30-points",
        "market_question": "Will LeBron James score 26+ points?",
        "outcomes": {"Yes": {"implied_prob": 47.0}, "No": {"implied_prob": 53.0}},
    }
    rows = sd.match_player_props_cross_venue(
        [_book_prop(line=25.5, consensus_devig=52.0)],
        [],
        poly_markets=[pm],
    )
    assert rows[0]["polymarket"] is not None
    assert rows[0]["polymarket"]["yes_prob"] == 47.0
    assert rows[0]["polymarket"]["trade_url"] == "https://polymarket.com/market/lebron-30-points"
    assert rows[0]["divergences"]["polymarket"] == 5.0  # 52 - 47


def test_match_sorts_signals_first():
    rows = sd.match_player_props_cross_venue(
        [
            _book_prop(player="Joe", consensus_devig=50.0),       # no signal
            _book_prop(player="Steph", consensus_devig=60.0),     # bigger signal
            _book_prop(player="LeBron", consensus_devig=55.0),    # smaller signal
        ],
        [
            _kalshi_prop(player="Joe", yes_prob=49.0),
            _kalshi_prop(player="Steph", yes_prob=42.0),  # 18pp gap
            _kalshi_prop(player="LeBron", yes_prob=48.0),  # 7pp gap
        ],
    )
    assert rows[0]["player_norm"] == "stephen curry"  # biggest divergence
    assert rows[-1]["player_norm"] == "joe"           # no signal, smallest
