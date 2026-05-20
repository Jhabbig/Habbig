"""Signal computation over the persisted filings.

All signals read from SQLite — they don't fetch. They're cheap enough
that the server calls them on demand, with a 60s response cache one
level up in `server.py`.

Key signals:
  - insider_clusters(window_days, min_buyers): tickers where >=N distinct
    insiders made open-market purchases in the window. Weighted by
    seniority (Officer > Director > 10% Owner > Other).
  - recent_insider_buys(window_days, min_value_usd)
  - recent_activist_stakes(window_days)
  - recent_ma_events(window_days)
  - ticker_synthesis(ticker): composite view of one ticker
  - whale_leaderboard(window_days): most prolific filers (insiders + activists)
"""

from __future__ import annotations

from typing import Any

from db import connect

SENIORITY_WEIGHT = {
    "Officer":    3.0,
    "Director":   2.0,
    "10% Owner":  1.5,
    "Other":      1.0,
}


def _seniority_score(relation: str) -> float:
    if not relation:
        return 1.0
    best = 1.0
    for label, w in SENIORITY_WEIGHT.items():
        if label in relation:
            best = max(best, w)
    return best


def insider_clusters(window_days: int = 30, min_buyers: int = 3) -> list[dict]:
    """Tickers with N+ distinct insider buyers in the window."""
    import skill as skill_mod  # lazy import to avoid circular at startup

    with connect() as cx:
        rows = cx.execute(
            """
            SELECT
                issuer_ticker, issuer_name, issuer_cik,
                reporter_cik, reporter_name, reporter_relation,
                shares, price, value_usd, txn_date, filing_url
            FROM insider_txn
            WHERE is_buy = 1
              AND issuer_ticker IS NOT NULL
              AND filed_at >= datetime('now', ? )
            """,
            (f"-{int(window_days)} days",),
        ).fetchall()

    by_ticker: dict[str, dict[str, Any]] = {}
    all_filer_ids: set[str] = set()
    for r in rows:
        t = r["issuer_ticker"]
        if not t:
            continue
        bucket = by_ticker.setdefault(t, {
            "ticker": t,
            "issuer_name": r["issuer_name"],
            "issuer_cik": r["issuer_cik"],
            "buyers": {},
            "total_value_usd": 0.0,
            "total_shares": 0.0,
            "weighted_score": 0.0,
            "latest_txn": "",
            "sample_filing_url": r["filing_url"],
        })
        bid = r["reporter_cik"] or r["reporter_name"]
        if r["reporter_cik"]:
            all_filer_ids.add(r["reporter_cik"])
        bucket["buyers"][bid] = {
            "cik": r["reporter_cik"],
            "name": r["reporter_name"],
            "relation": r["reporter_relation"],
        }
        bucket["total_value_usd"] += float(r["value_usd"] or 0)
        bucket["total_shares"]    += float(r["shares"] or 0)
        bucket["weighted_score"]  += _seniority_score(r["reporter_relation"] or "")
        if (r["txn_date"] or "") > bucket["latest_txn"]:
            bucket["latest_txn"] = r["txn_date"] or ""

    skill_map = skill_mod.skill_for_filers("insider", all_filer_ids)

    out: list[dict] = []
    for t, b in by_ticker.items():
        if len(b["buyers"]) < min_buyers:
            continue
        buyers = list(b["buyers"].values())
        for buyer in buyers:
            buyer["skill"] = skill_map.get(buyer["cik"] or "")
        out.append({
            "ticker":          t,
            "issuer_name":     b["issuer_name"],
            "issuer_cik":      b["issuer_cik"],
            "n_buyers":        len(buyers),
            "buyers":          buyers,
            "total_value_usd": round(b["total_value_usd"], 2),
            "total_shares":    round(b["total_shares"], 2),
            "weighted_score":  round(b["weighted_score"], 2),
            "latest_txn":      b["latest_txn"],
            "sample_filing_url": b["sample_filing_url"],
        })
    out.sort(key=lambda r: (r["n_buyers"], r["weighted_score"], r["total_value_usd"]), reverse=True)
    return out


def recent_insider_buys(window_days: int = 7, min_value_usd: float = 100_000) -> list[dict]:
    with connect() as cx:
        rows = cx.execute(
            """
            SELECT accession, filed_at, reporter_name, reporter_relation,
                   issuer_ticker, issuer_name, txn_date, txn_code,
                   shares, price, value_usd, filing_url
            FROM insider_txn
            WHERE is_buy = 1
              AND filed_at >= datetime('now', ? )
              AND COALESCE(value_usd, 0) >= ?
            ORDER BY filed_at DESC
            LIMIT 500
            """,
            (f"-{int(window_days)} days", float(min_value_usd)),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_activist_stakes(window_days: int = 14) -> list[dict]:
    with connect() as cx:
        rows = cx.execute(
            """
            SELECT accession, filed_at, filer_name, filer_cik,
                   issuer_name, issuer_ticker, issuer_cik,
                   pct_owned, shares_owned, filing_type, filing_url
            FROM activist_stake
            WHERE filed_at >= datetime('now', ? )
            ORDER BY filed_at DESC
            LIMIT 200
            """,
            (f"-{int(window_days)} days",),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_ma_events(window_days: int = 7, min_score: float = 2.0) -> list[dict]:
    with connect() as cx:
        rows = cx.execute(
            """
            SELECT accession, filed_at, issuer_name, issuer_ticker,
                   issuer_cik, items, headline, ma_score, filing_url
            FROM ma_event
            WHERE filed_at >= datetime('now', ? )
              AND ma_score >= ?
            ORDER BY ma_score DESC, filed_at DESC
            LIMIT 200
            """,
            (f"-{int(window_days)} days", float(min_score)),
        ).fetchall()
    return [dict(r) for r in rows]


def ticker_synthesis(ticker: str, window_days: int = 90) -> dict:
    """Composite view of one ticker.

    Combines: insider buy summary + activist filings + M&A 8-Ks + a single
    `synthesis_score` so the UI can rank tickers by 'most going on'.
    """
    t = (ticker or "").upper().strip()
    if not t:
        return {"ticker": "", "error": "ticker required"}

    cutoff = f"-{int(window_days)} days"
    with connect() as cx:
        insider_rows = cx.execute(
            """
            SELECT reporter_cik, reporter_name, reporter_relation,
                   txn_date, txn_code, shares, price, value_usd,
                   filed_at, filing_url, is_buy
            FROM insider_txn
            WHERE issuer_ticker = ? AND filed_at >= datetime('now', ?)
            ORDER BY filed_at DESC
            """,
            (t, cutoff),
        ).fetchall()

        activist_rows = cx.execute(
            """
            SELECT accession, filed_at, filer_name, filer_cik,
                   pct_owned, shares_owned, filing_type, filing_url
            FROM activist_stake
            WHERE issuer_ticker = ? AND filed_at >= datetime('now', ?)
            ORDER BY filed_at DESC
            """,
            (t, cutoff),
        ).fetchall()

        ma_rows = cx.execute(
            """
            SELECT accession, filed_at, items, headline, ma_score, filing_url
            FROM ma_event
            WHERE issuer_ticker = ? AND filed_at >= datetime('now', ?)
            ORDER BY filed_at DESC
            """,
            (t, cutoff),
        ).fetchall()

    insider_buys = [dict(r) for r in insider_rows if r["is_buy"]]
    insider_sells = [dict(r) for r in insider_rows if not r["is_buy"] and (r["txn_code"] == "S")]

    insider_score = sum(_seniority_score(r["reporter_relation"] or "") for r in insider_buys)
    activist_score = sum(2.0 + 0.05 * float(r["pct_owned"] or 0) for r in activist_rows)
    ma_score_sum = sum(float(r["ma_score"] or 0) for r in ma_rows)

    holders = ticker_holders(t, limit=50)
    congress = congress_by_ticker(t, limit=50)
    # Congress signal: count buys minus sells in the window.
    cong_buys  = sum(1 for c in congress if (c.get("transaction_type") or "").lower().startswith("p"))
    cong_sells = sum(1 for c in congress if (c.get("transaction_type") or "").lower().startswith("s"))
    congress_score = max(0.0, (cong_buys - cong_sells) * 0.75)
    # Fund signal: presence in 13F filings adds a small structural bonus.
    fund_score = min(2.0, len(holders) * 0.05)
    synthesis_score = round(
        insider_score + activist_score + ma_score_sum + congress_score + fund_score,
        2,
    )

    return {
        "ticker":           t,
        "window_days":      window_days,
        "synthesis_score":  synthesis_score,
        "insider_buy_count":  len(insider_buys),
        "insider_sell_count": len(insider_sells),
        "activist_count":     len(activist_rows),
        "ma_event_count":     len(ma_rows),
        "fund_holder_count":  len(holders),
        "congress_buy_count": cong_buys,
        "congress_sell_count": cong_sells,
        "insider_buys":     insider_buys[:50],
        "insider_sells":    insider_sells[:50],
        "activist_filings": [dict(r) for r in activist_rows],
        "ma_events":        [dict(r) for r in ma_rows],
        "fund_holders":     holders,
        "congress_trades":  congress,
    }


def hot_leaderboard(window_days: int = 30, limit: int = 50) -> list[dict]:
    """Cross-signal hot tickers: ranked by combined synthesis score.

    Pulls every ticker that appears in any of the three feeds in the
    window, runs the same composite scoring used by `ticker_synthesis`,
    and returns the top N. This is the "most going on right now" view.
    """
    cutoff = f"-{int(window_days)} days"
    with connect() as cx:
        rows = cx.execute(
            """
            SELECT issuer_ticker AS ticker FROM insider_txn
              WHERE issuer_ticker IS NOT NULL AND is_buy = 1 AND filed_at >= datetime('now', ?)
            UNION
            SELECT issuer_ticker AS ticker FROM activist_stake
              WHERE issuer_ticker IS NOT NULL AND filed_at >= datetime('now', ?)
            UNION
            SELECT issuer_ticker AS ticker FROM ma_event
              WHERE issuer_ticker IS NOT NULL AND filed_at >= datetime('now', ?)
            UNION
            SELECT ticker FROM congress_trade
              WHERE ticker IS NOT NULL AND disclosure_date >= datetime('now', ?)
            """,
            (cutoff, cutoff, cutoff, cutoff),
        ).fetchall()

    out: list[dict] = []
    for r in rows:
        t = r["ticker"]
        if not t:
            continue
        s = ticker_synthesis(t, window_days=window_days)
        if (s.get("synthesis_score") or 0) <= 0:
            continue
        out.append({
            "ticker":              t,
            "score":               s["synthesis_score"],
            "insider_buy_count":   s["insider_buy_count"],
            "insider_sell_count":  s["insider_sell_count"],
            "activist_count":      s["activist_count"],
            "ma_event_count":      s["ma_event_count"],
            "fund_holder_count":   s.get("fund_holder_count", 0),
            "congress_buy_count":  s.get("congress_buy_count", 0),
            "congress_sell_count": s.get("congress_sell_count", 0),
            # First filing URL from any feed, for quick navigation
            "sample_url": (
                (s["insider_buys"][0]["filing_url"]   if s["insider_buys"]    else None)
                or (s["activist_filings"][0]["filing_url"] if s["activist_filings"] else None)
                or (s["ma_events"][0]["filing_url"]   if s["ma_events"]       else None)
                or (s.get("congress_trades", [{}])[0].get("source_url") if s.get("congress_trades") else None)
            ),
        })
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:int(limit)]


def fund_list(limit: int = 100) -> list[dict]:
    """Funds known to the system, ranked by most recent filing."""
    with connect() as cx:
        rows = cx.execute(
            """
            SELECT fund_cik, fund_name,
                   MAX(period_of_report) AS latest_period,
                   MAX(filed_at)         AS latest_filed,
                   COUNT(*)              AS filing_count,
                   MAX(total_value)      AS latest_value,
                   MAX(holding_count)    AS latest_holdings
            FROM fund_filing
            GROUP BY fund_cik
            ORDER BY latest_filed DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def fund_holdings(fund_cik: str, limit: int = 200) -> dict:
    """Most recent 13F holdings for a fund."""
    if not fund_cik:
        return {"fund_cik": "", "error": "fund_cik required"}
    with connect() as cx:
        f = cx.execute(
            """
            SELECT accession, filed_at, period_of_report, fund_name,
                   total_value, holding_count, filing_url
            FROM fund_filing
            WHERE fund_cik = ?
            ORDER BY filed_at DESC
            LIMIT 1
            """,
            (fund_cik,),
        ).fetchone()
        if not f:
            return {"fund_cik": fund_cik, "filing": None, "holdings": []}
        h = cx.execute(
            """
            SELECT line_no, cusip, issuer_name, title_of_class, issuer_ticker,
                   value, shares, shares_type, put_call
            FROM fund_holding
            WHERE accession = ?
            ORDER BY value DESC NULLS LAST
            LIMIT ?
            """,
            (f["accession"], int(limit)),
        ).fetchall()
    return {
        "fund_cik": fund_cik,
        "filing": dict(f),
        "holdings": [dict(r) for r in h],
    }


def holding_changes(window_days: int = 120, change_type: str | None = None, limit: int = 100) -> list[dict]:
    """Quarter-over-quarter position changes across all tracked funds.

    For each fund, compare its latest filing to the prior filing and emit
    rows for new positions / exits / size changes. `change_type` filters
    to 'new', 'exit', 'increase', or 'decrease'.
    """
    valid = {"new", "exit", "increase", "decrease"}
    if change_type and change_type not in valid:
        change_type = None

    with connect() as cx:
        funds = cx.execute(
            """
            SELECT fund_cik, fund_name,
                   MAX(filed_at) AS latest_filed
            FROM fund_filing
            WHERE filed_at >= datetime('now', ?)
            GROUP BY fund_cik
            """,
            (f"-{int(window_days)} days",),
        ).fetchall()

        out: list[dict] = []
        for f in funds:
            cik = f["fund_cik"]
            two = cx.execute(
                """
                SELECT accession, period_of_report, filed_at
                FROM fund_filing
                WHERE fund_cik = ?
                ORDER BY filed_at DESC
                LIMIT 2
                """,
                (cik,),
            ).fetchall()
            if len(two) < 1:
                continue
            latest = two[0]
            prior  = two[1] if len(two) == 2 else None

            cur_rows = cx.execute(
                "SELECT cusip, issuer_name, issuer_ticker, value, shares FROM fund_holding WHERE accession = ?",
                (latest["accession"],),
            ).fetchall()
            prior_rows = cx.execute(
                "SELECT cusip, issuer_name, issuer_ticker, value, shares FROM fund_holding WHERE accession = ?",
                (prior["accession"],),
            ).fetchall() if prior else []

            prior_map = {r["cusip"]: r for r in prior_rows if r["cusip"]}
            cur_map   = {r["cusip"]: r for r in cur_rows if r["cusip"]}

            for cusip, cur in cur_map.items():
                if cusip not in prior_map:
                    out.append({
                        "change_type": "new",
                        "fund_cik": cik, "fund_name": f["fund_name"],
                        "period":   latest["period_of_report"],
                        "cusip":    cusip,
                        "issuer_name":   cur["issuer_name"],
                        "issuer_ticker": cur["issuer_ticker"],
                        "delta_value":   cur["value"],
                        "delta_shares":  cur["shares"],
                        "from_value": 0,
                        "to_value":   cur["value"],
                    })
                else:
                    p = prior_map[cusip]
                    cv = (cur["value"]   or 0)
                    pv = (p["value"]     or 0)
                    cs = (cur["shares"]  or 0)
                    ps = (p["shares"]    or 0)
                    if cs == ps and cv == pv:
                        continue
                    ctype = "increase" if cs > ps else "decrease"
                    out.append({
                        "change_type": ctype,
                        "fund_cik": cik, "fund_name": f["fund_name"],
                        "period":   latest["period_of_report"],
                        "cusip":    cusip,
                        "issuer_name":   cur["issuer_name"],
                        "issuer_ticker": cur["issuer_ticker"],
                        "delta_value":   cv - pv,
                        "delta_shares":  cs - ps,
                        "from_value":    pv,
                        "to_value":      cv,
                    })

            for cusip, p in prior_map.items():
                if cusip not in cur_map:
                    out.append({
                        "change_type": "exit",
                        "fund_cik": cik, "fund_name": f["fund_name"],
                        "period":   latest["period_of_report"],
                        "cusip":    cusip,
                        "issuer_name":   p["issuer_name"],
                        "issuer_ticker": p["issuer_ticker"],
                        "delta_value":   -(p["value"] or 0),
                        "delta_shares":  -(p["shares"] or 0),
                        "from_value":    p["value"],
                        "to_value":      0,
                    })

    if change_type:
        out = [r for r in out if r["change_type"] == change_type]
    # Rank by absolute $ delta — biggest swings first.
    out.sort(key=lambda r: abs(r.get("delta_value") or 0), reverse=True)
    return out[: int(limit)]


def ticker_holders(ticker: str, limit: int = 100) -> list[dict]:
    """Funds holding a given ticker (latest filing per fund)."""
    t = (ticker or "").upper().strip()
    if not t:
        return []
    with connect() as cx:
        rows = cx.execute(
            """
            SELECT h.fund_cik, f.fund_name, h.value, h.shares, h.shares_type,
                   h.put_call, h.cusip, h.title_of_class, f.period_of_report,
                   f.filed_at, f.filing_url
            FROM fund_holding h
            JOIN fund_filing  f ON f.accession = h.accession
            WHERE h.issuer_ticker = ?
              AND f.accession IN (
                  SELECT accession FROM fund_filing ff
                  WHERE ff.fund_cik = h.fund_cik
                  ORDER BY ff.filed_at DESC LIMIT 1
              )
            ORDER BY h.value DESC NULLS LAST
            LIMIT ?
            """,
            (t, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_congress_trades(window_days: int = 30, limit: int = 200, chamber: str | None = None) -> list[dict]:
    cutoff = f"-{int(window_days)} days"
    params: list = [cutoff]
    sql = (
        "SELECT * FROM congress_trade "
        "WHERE disclosure_date >= datetime('now', ?)"
    )
    if chamber in ("House", "Senate"):
        sql += " AND chamber = ?"
        params.append(chamber)
    sql += " ORDER BY disclosure_date DESC LIMIT ?"
    params.append(int(limit))
    with connect() as cx:
        rows = cx.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def congress_by_ticker(ticker: str, limit: int = 200) -> list[dict]:
    t = (ticker or "").upper().strip()
    if not t:
        return []
    with connect() as cx:
        rows = cx.execute(
            "SELECT * FROM congress_trade WHERE ticker = ? "
            "ORDER BY disclosure_date DESC LIMIT ?",
            (t, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def whale_leaderboard(window_days: int = 90) -> list[dict]:
    """Most active filers across both insider txns and activist stakes."""
    cutoff = f"-{int(window_days)} days"
    with connect() as cx:
        insider = cx.execute(
            """
            SELECT reporter_cik AS cik, reporter_name AS name,
                   COUNT(*) AS txn_count,
                   SUM(CASE WHEN is_buy = 1 THEN COALESCE(value_usd,0) ELSE 0 END) AS total_buys_usd,
                   SUM(CASE WHEN is_buy = 0 AND txn_code = 'S' THEN COALESCE(value_usd,0) ELSE 0 END) AS total_sells_usd
            FROM insider_txn
            WHERE filed_at >= datetime('now', ?)
              AND reporter_cik IS NOT NULL
            GROUP BY reporter_cik
            ORDER BY txn_count DESC
            LIMIT 100
            """,
            (cutoff,),
        ).fetchall()

        activist = cx.execute(
            """
            SELECT filer_cik AS cik, filer_name AS name,
                   COUNT(*) AS stake_count,
                   AVG(pct_owned) AS avg_pct
            FROM activist_stake
            WHERE filed_at >= datetime('now', ?)
              AND filer_cik IS NOT NULL
            GROUP BY filer_cik
            ORDER BY stake_count DESC
            LIMIT 100
            """,
            (cutoff,),
        ).fetchall()

    return {
        "insiders":  [dict(r) for r in insider],
        "activists": [dict(r) for r in activist],
    }
