"""Outcome labeling + Bayesian skill leaderboards.

A filer's "outcome" for a filing is a binary label:
  win  = directional return outperformed SPY by > 0 over `horizon_days`
  loss = otherwise

For "buy" direction, the outperformance check is `ticker_return > spy_return`.
For "sell" direction, it's `ticker_return < spy_return` (they got out and
were proved right).

Filer identities we currently score:
  insider  — filer_id = reporter_cik
  activist — filer_id = filer_cik
  congress — filer_id = representative (name, normalised)

13F fund skill is deferred (CUSIP→ticker resolution not in place).

`run_pass()` is idempotent — it skips outcomes we've already computed.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Iterable

import bayesian
import db
import prices

log = logging.getLogger("skill")

DEFAULT_HORIZON_DAYS = 30
PER_PASS_LIMIT = 200


def _today() -> str:
    return dt.date.today().isoformat()


def _days_ago(date_str: str) -> int:
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return 0
    return (dt.date.today() - d).days


def _shift_days(date_str: str, days: int) -> str:
    d = dt.date.fromisoformat(date_str[:10])
    return (d + dt.timedelta(days=days)).isoformat()


def _candidates(horizon_days: int = DEFAULT_HORIZON_DAYS, limit: int = PER_PASS_LIMIT) -> list[dict]:
    """Gather (filer_type, filer_id, source_id, direction, ticker, filing_date)
    rows that are old enough to label but don't yet have an outcome.
    """
    cutoff_date = _shift_days(_today(), -horizon_days)
    rows: list[dict] = []

    with db.connect() as cx:
        # Insiders — open-market purchases (is_buy=1) and sales (txn_code='S').
        for r in cx.execute(
            """
            SELECT accession, line_no, reporter_cik, issuer_ticker,
                   COALESCE(txn_date, substr(filed_at, 1, 10)) AS filing_date,
                   txn_code, is_buy
            FROM insider_txn
            WHERE issuer_ticker IS NOT NULL
              AND COALESCE(txn_date, substr(filed_at, 1, 10)) <= ?
              AND reporter_cik IS NOT NULL AND reporter_cik != ''
              AND (is_buy = 1 OR txn_code = 'S')
            LIMIT ?
            """,
            (cutoff_date, limit * 4),
        ).fetchall():
            source_id = f"{r['accession']}:{r['line_no']}"
            if db.have_outcome("insider", r["reporter_cik"], source_id, horizon_days):
                continue
            direction = "buy" if r["is_buy"] else "sell"
            rows.append({
                "filer_type": "insider",
                "filer_id":   r["reporter_cik"],
                "source_id":  source_id,
                "direction":  direction,
                "ticker":     r["issuer_ticker"],
                "filing_date": r["filing_date"],
            })

        for r in cx.execute(
            """
            SELECT accession, filer_cik, issuer_ticker, substr(filed_at, 1, 10) AS filing_date
            FROM activist_stake
            WHERE issuer_ticker IS NOT NULL
              AND filer_cik IS NOT NULL AND filer_cik != ''
              AND substr(filed_at, 1, 10) <= ?
            LIMIT ?
            """,
            (cutoff_date, limit * 2),
        ).fetchall():
            source_id = r["accession"]
            if db.have_outcome("activist", r["filer_cik"], source_id, horizon_days):
                continue
            rows.append({
                "filer_type": "activist",
                "filer_id":   r["filer_cik"],
                "source_id":  source_id,
                "direction":  "buy",
                "ticker":     r["issuer_ticker"],
                "filing_date": r["filing_date"],
            })

        for r in cx.execute(
            """
            SELECT transaction_id, representative, ticker, transaction_date, transaction_type
            FROM congress_trade
            WHERE ticker IS NOT NULL
              AND representative IS NOT NULL AND representative != ''
              AND transaction_date IS NOT NULL
              AND transaction_date <= ?
            LIMIT ?
            """,
            (cutoff_date, limit * 2),
        ).fetchall():
            t = (r["transaction_type"] or "").lower()
            if t.startswith("p"):
                direction = "buy"
            elif t.startswith("s"):
                direction = "sell"
            else:
                continue
            filer = _normalise_name(r["representative"])
            if db.have_outcome("congress", filer, r["transaction_id"], horizon_days):
                continue
            rows.append({
                "filer_type": "congress",
                "filer_id":   filer,
                "source_id":  r["transaction_id"],
                "direction":  direction,
                "ticker":     r["ticker"],
                "filing_date": r["transaction_date"],
            })

    # Skill labeling is most informative when prioritised by recency.
    rows.sort(key=lambda r: r["filing_date"], reverse=True)
    return rows[:limit]


def _normalise_name(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


async def run_pass(horizon_days: int = DEFAULT_HORIZON_DAYS, limit: int = PER_PASS_LIMIT) -> dict:
    """Label up to `limit` pending outcomes. Returns a small summary."""
    cands = _candidates(horizon_days=horizon_days, limit=limit)
    if not cands:
        return {"labeled": 0, "skipped_no_price": 0, "candidates": 0}

    # Ensure we have prices locally for every ticker + the benchmark.
    tickers = sorted({c["ticker"].upper() for c in cands})
    await prices.ensure_prices_for(tickers)

    labeled = 0
    skipped = 0
    for c in cands:
        try:
            ok = _label_one(c, horizon_days)
        except Exception as e:
            log.info("label failed for %s/%s: %s", c["filer_type"], c["source_id"], e)
            ok = False
        if ok:
            labeled += 1
        else:
            skipped += 1
    return {"labeled": labeled, "skipped_no_price": skipped, "candidates": len(cands)}


def _label_one(c: dict, horizon_days: int) -> bool:
    ticker = c["ticker"].upper()
    filing_date = c["filing_date"][:10]
    target_date = _shift_days(filing_date, horizon_days)

    p0 = db.get_close_on_or_after(ticker, filing_date)
    p1 = db.get_close_on_or_after(ticker, target_date)
    spy0 = db.get_close_on_or_after(prices.BENCHMARK_TICKER, filing_date)
    spy1 = db.get_close_on_or_after(prices.BENCHMARK_TICKER, target_date)

    if not (p0 and p1 and spy0 and spy1):
        return False
    if p0[1] <= 0 or spy0[1] <= 0:
        return False

    r_ticker = (p1[1] / p0[1]) - 1.0
    r_spy    = (spy1[1] / spy0[1]) - 1.0
    alpha    = r_ticker - r_spy

    if c["direction"] == "buy":
        win = 1 if alpha > 0 else 0
    else:  # sell
        win = 1 if alpha < 0 else 0

    db.upsert_filer_outcome({
        "filer_type":    c["filer_type"],
        "filer_id":      c["filer_id"],
        "source_id":     c["source_id"],
        "direction":     c["direction"],
        "ticker":        ticker,
        "filing_date":   filing_date,
        "horizon_days":  horizon_days,
        "return_pct":    round(r_ticker * 100, 4),
        "benchmark_pct": round(r_spy * 100, 4),
        "alpha_pct":     round(alpha * 100, 4),
        "win":           win,
        "computed_at":   dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })
    return True


# ─── leaderboard queries ─────────────────────────────────────────────

def _filer_display_names() -> dict[tuple[str, str], str]:
    """Map (filer_type, filer_id) → human display name."""
    out: dict[tuple[str, str], str] = {}
    with db.connect() as cx:
        for r in cx.execute(
            "SELECT DISTINCT reporter_cik AS id, reporter_name AS name "
            "FROM insider_txn WHERE reporter_cik IS NOT NULL"
        ).fetchall():
            out[("insider", r["id"])] = r["name"] or r["id"]
        for r in cx.execute(
            "SELECT DISTINCT filer_cik AS id, filer_name AS name "
            "FROM activist_stake WHERE filer_cik IS NOT NULL"
        ).fetchall():
            out[("activist", r["id"])] = r["name"] or r["id"]
        # Congress filer_id is already a name.
    return out


def leaderboard(filer_type: str | None = None, min_n: int = 5, limit: int = 50,
                horizon_days: int = DEFAULT_HORIZON_DAYS) -> list[dict]:
    """Top filers by posterior mean (high-confidence skilled first)."""
    where = ["horizon_days = ?"]
    params: list = [horizon_days]
    if filer_type in ("insider", "activist", "congress"):
        where.append("filer_type = ?")
        params.append(filer_type)
    sql = (
        "SELECT filer_type, filer_id, "
        "       SUM(win)         AS wins, "
        "       SUM(1 - win)     AS losses, "
        "       COUNT(*)         AS n, "
        "       AVG(alpha_pct)   AS avg_alpha_pct "
        "FROM filer_outcome WHERE " + " AND ".join(where) +
        " GROUP BY filer_type, filer_id"
    )
    with db.connect() as cx:
        rows = cx.execute(sql, params).fetchall()

    names = _filer_display_names()
    out: list[dict] = []
    for r in rows:
        if (r["n"] or 0) < min_n:
            continue
        est = bayesian.estimate(int(r["wins"] or 0), int(r["losses"] or 0))
        display = names.get((r["filer_type"], r["filer_id"]), r["filer_id"])
        out.append({
            "filer_type": r["filer_type"],
            "filer_id":   r["filer_id"],
            "filer_name": display,
            **est.as_dict(),
            "avg_alpha_pct": round(r["avg_alpha_pct"] or 0, 3),
        })

    out.sort(key=lambda r: (
        not r["high_confidence_skilled"],   # high-confidence first
        -r["ci_lower"],                     # then by lower CI bound (conservative ranking)
        -r["posterior_mean"],
    ))
    return out[:int(limit)]


def filer_detail(filer_type: str, filer_id: str, horizon_days: int = DEFAULT_HORIZON_DAYS,
                 recent_limit: int = 50) -> dict:
    """Skill estimate + recent labeled outcomes for one filer."""
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT * FROM filer_outcome "
            "WHERE filer_type = ? AND filer_id = ? AND horizon_days = ? "
            "ORDER BY filing_date DESC LIMIT ?",
            (filer_type, filer_id, horizon_days, int(recent_limit)),
        ).fetchall()
    if not rows:
        return {
            "filer_type": filer_type, "filer_id": filer_id,
            "outcomes": [], "skill": bayesian.estimate(0, 0).as_dict(),
        }
    wins   = sum(int(r["win"]) for r in rows)
    losses = len(rows) - wins
    est = bayesian.estimate(wins, losses)
    names = _filer_display_names()
    return {
        "filer_type": filer_type,
        "filer_id":   filer_id,
        "filer_name": names.get((filer_type, filer_id), filer_id),
        "skill":      est.as_dict(),
        "outcomes":   [dict(r) for r in rows],
    }


def skill_for_filers(filer_type: str, filer_ids: Iterable[str],
                     horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict[str, dict]:
    """Batch skill lookup. Returns {filer_id: skill_dict}. Used by UI to badge."""
    ids = [i for i in {*filer_ids} if i]
    if not ids:
        return {}
    placeholders = ",".join(["?"] * len(ids))
    sql = (
        f"SELECT filer_id, SUM(win) AS wins, SUM(1 - win) AS losses "
        f"FROM filer_outcome WHERE filer_type = ? AND horizon_days = ? "
        f"AND filer_id IN ({placeholders}) GROUP BY filer_id"
    )
    with db.connect() as cx:
        rows = cx.execute(sql, [filer_type, horizon_days, *ids]).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        est = bayesian.estimate(int(r["wins"] or 0), int(r["losses"] or 0))
        out[r["filer_id"]] = est.as_dict()
    return out
