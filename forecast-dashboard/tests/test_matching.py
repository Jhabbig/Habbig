import pytest

import db
import matching

FED_END = "2026-09-17T18:00:00Z"
P_CUT = "Will the Fed cut rates by 25 bps at the September 2026 meeting?"
P_HOLD = "Will the Fed hold rates steady at the September 2026 meeting?"
K_CUT = "Will the Federal Reserve Cut rates by 25bps at their September 2026 meeting?"
K_HIKE = "Will the Federal Reserve Hike rates by 25bps at their September 2026 meeting?"

W_END = "2026-09-01T23:59:00Z"
P_WEATHER = "Will the highest temperature in NYC on September 1, 2026 be 46° or higher?"
K_WEATHER_OVER = ("KXHIGHNY-26SEP01-B45.5", "Will the high temp in New York be above 45.5° on Sep 1, 2026?")
K_WEATHER_UNDER = ("KXHIGHNY-26SEP01-T45.5", "Will the high temp in New York be 45.5° or below on Sep 1, 2026?")

FUZZY_Q = "Will SpaceX complete 3 successful Starship launches in October 2026?"


def _mk(conn, venue, venue_id, question, end_date, **kw):
    m = {
        "venue": venue,
        "venue_id": venue_id,
        "question": question,
        "end_date": end_date,
        "category": kw.get("category"),
        "event_ticker": kw.get("event_ticker"),
        "url": kw.get("url", f"https://example.com/{venue}/{venue_id}"),
    }
    return db.upsert_market(conn, m)


def _snap(conn, uid, baseline, spread):
    db.insert_snapshot(conn, {"market_uid": uid, "baseline_prob": baseline, "spread": spread})


def test_fed_bucket_join_links_only_across_venues(conn):
    p_cut = _mk(conn, "polymarket", "p-cut", P_CUT, FED_END)
    _mk(conn, "polymarket", "p-hold", P_HOLD, FED_END)
    k_cut = _mk(conn, "kalshi", "KXFED-26SEP-C25", K_CUT, FED_END)
    _mk(conn, "kalshi", "KXFED-26SEP-H25", K_HIKE, FED_END)

    matching.match(conn)

    links = db.links(conn)
    assert len(links) == 1
    link = links[0]
    assert (link["poly_uid"], link["kalshi_uid"]) == (p_cut, k_cut)
    assert link["method"] == "rulepack:fed"
    assert link["score"] == 1.0
    assert link["status"] == "auto"
    for row in links:
        assert row["poly_uid"].startswith("polymarket:")
        assert row["kalshi_uid"].startswith("kalshi:")


def test_weather_tuple_join(conn):
    p_uid = _mk(conn, "polymarket", "0xweather", P_WEATHER, W_END)
    k_over = _mk(conn, "kalshi", K_WEATHER_OVER[0], K_WEATHER_OVER[1], W_END)
    k_under = _mk(conn, "kalshi", K_WEATHER_UNDER[0], K_WEATHER_UNDER[1], W_END)

    matching.match(conn)

    links = db.links(conn)
    assert len(links) == 1
    link = links[0]
    assert (link["poly_uid"], link["kalshi_uid"]) == (p_uid, k_over)
    assert link["method"] == "rulepack:weather"
    assert link["status"] == "auto"
    assert not any(row["kalshi_uid"] == k_under for row in links)


def test_weather_opposite_tail_never_links(conn):
    # Live catalog: KXHIGHNY T96 is ">96°" (top tail). The ticker's T suffix
    # must never key it as an under market and auto-link the opposite outcome.
    _mk(conn, "polymarket", "0xunder96", "Will the high temperature in NYC be 96° or lower on September 1, 2026?", W_END)
    _mk(conn, "kalshi", "KXHIGHNY-26SEP01-T96", "Will the **high temp in NYC** be >96° on Sep 1, 2026? — 97° or above", W_END, event_ticker="KXHIGHNY-26SEP01")

    matching.match(conn)

    assert db.links(conn) == []


def test_weather_top_tail_links_to_equivalent_poly(conn):
    p_uid = _mk(conn, "polymarket", "0xover97", "Will the high temperature in NYC be 97° or higher on September 1, 2026?", W_END)
    k_uid = _mk(conn, "kalshi", "KXHIGHNY-26SEP01-T96", "Will the **high temp in NYC** be >96° on Sep 1, 2026? — 97° or above", W_END, event_ticker="KXHIGHNY-26SEP01")

    matching.match(conn)

    links = db.links(conn)
    assert len(links) == 1
    assert (links[0]["poly_uid"], links[0]["kalshi_uid"]) == (p_uid, k_uid)
    assert links[0]["method"] == "rulepack:weather"
    assert links[0]["status"] == "auto"


def test_weather_range_bracket_links_only_to_range(conn):
    # Live catalog: B95.5 is the "95° to 96°" range bracket, not "above 95.5".
    p_rng = _mk(conn, "polymarket", "0xrange", "Will the high temperature in NYC be 95-96° on September 1, 2026?", W_END)
    _mk(conn, "polymarket", "0xover96", "Will the high temperature in NYC be 96° or higher on September 1, 2026?", W_END)
    k_uid = _mk(conn, "kalshi", "KXHIGHNY-26SEP01-B95.5", "Will the **high temp in NYC** be 95-96° on Sep 1, 2026? — 95° to 96°", W_END, event_ticker="KXHIGHNY-26SEP01")

    matching.match(conn)

    links = db.links(conn)
    assert len(links) == 1
    assert (links[0]["poly_uid"], links[0]["kalshi_uid"]) == (p_rng, k_uid)
    assert links[0]["method"] == "rulepack:weather"
    assert links[0]["status"] == "auto"


def test_fuzzy_suggests_never_auto_even_at_perfect_score(conn):
    p_uid = _mk(conn, "polymarket", "0xspacex", FUZZY_Q, "2026-10-31T00:00:00Z")
    k_uid = _mk(conn, "kalshi", "KXSPACEX-26", FUZZY_Q, "2026-10-31T12:00:00Z")

    matching.match(conn)

    links = db.links(conn)
    assert len(links) == 1
    link = links[0]
    assert (link["poly_uid"], link["kalshi_uid"]) == (p_uid, k_uid)
    assert link["method"] == "fuzzy"
    assert link["score"] == pytest.approx(1.0)
    assert link["status"] == "suggested"
    assert link["status"] != "auto"


def test_rejected_pair_never_resurfaces(conn):
    p_fed = _mk(conn, "polymarket", "p-cut", P_CUT, FED_END)
    k_fed = _mk(conn, "kalshi", "KXFED-26SEP-C25", K_CUT, FED_END)
    p_fz = _mk(conn, "polymarket", "0xspacex", FUZZY_Q, "2026-10-31T00:00:00Z")
    k_fz = _mk(conn, "kalshi", "KXSPACEX-26", FUZZY_Q, "2026-10-31T12:00:00Z")

    matching.match(conn)
    by_pair = {(r["poly_uid"], r["kalshi_uid"]): r for r in db.links(conn)}
    assert by_pair[(p_fed, k_fed)]["status"] == "auto"
    assert by_pair[(p_fz, k_fz)]["status"] == "suggested"

    assert db.set_link_status(conn, p_fed, k_fed, "rejected")
    assert db.set_link_status(conn, p_fz, k_fz, "rejected")

    matching.match(conn)

    links = db.links(conn)
    assert len(links) == 2
    for row in links:
        assert row["status"] == "rejected"


def test_confirmed_link_left_alone(conn):
    p_uid = _mk(conn, "polymarket", "0xspacex", FUZZY_Q, "2026-10-31T00:00:00Z")
    k_uid = _mk(conn, "kalshi", "KXSPACEX-26", FUZZY_Q, "2026-10-31T12:00:00Z")

    matching.match(conn)
    assert db.set_link_status(conn, p_uid, k_uid, "confirmed")

    matching.match(conn)

    links = db.links(conn)
    assert len(links) == 1
    assert links[0]["status"] == "confirmed"
    assert links[0]["method"] == "fuzzy"


def test_disagreement_threshold_respects_spread_term(conn):
    p_narrow = _mk(conn, "polymarket", "p-narrow", "Narrow spread market?", FED_END)
    k_narrow = _mk(conn, "kalshi", "K-NARROW", "Narrow spread market?", FED_END)
    _snap(conn, p_narrow, 0.60, 0.01)
    _snap(conn, k_narrow, 0.50, 0.01)
    db.upsert_link(conn, p_narrow, k_narrow, "rulepack:fed", 1.0, "confirmed")

    p_wide = _mk(conn, "polymarket", "p-wide", "Wide spread market?", FED_END)
    k_wide = _mk(conn, "kalshi", "K-WIDE", "Wide spread market?", FED_END)
    _snap(conn, p_wide, 0.60, 0.10)
    _snap(conn, k_wide, 0.52, 0.10)
    db.upsert_link(conn, p_wide, k_wide, "rulepack:fed", 1.0, "auto")

    p_sug = _mk(conn, "polymarket", "p-sug", "Suggested-only market?", FED_END)
    k_sug = _mk(conn, "kalshi", "K-SUG", "Suggested-only market?", FED_END)
    _snap(conn, p_sug, 0.90, 0.01)
    _snap(conn, k_sug, 0.10, 0.01)
    db.upsert_link(conn, p_sug, k_sug, "fuzzy", 0.9, "suggested")

    rows = matching.disagreements(conn)

    assert [r["poly_uid"] for r in rows] == [p_narrow]
    row = rows[0]
    assert row["kalshi_uid"] == k_narrow
    assert row["gap"] == pytest.approx(0.10)
    assert row["delta"] == pytest.approx(0.10)
    assert row["poly_baseline"] == pytest.approx(0.60)
    assert row["kalshi_baseline"] == pytest.approx(0.50)
    assert row["method"] == "rulepack:fed"
    assert row["urls"]["polymarket"] == "https://example.com/polymarket/p-narrow"
    assert row["urls"]["kalshi"] == "https://example.com/kalshi/K-NARROW"
