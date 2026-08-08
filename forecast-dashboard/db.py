"""SQLite layer for forecast-dashboard.

All helpers take an explicit sqlite3.Connection as their first argument
(except get_conn, which creates one). Connections are cheap: callers open
one per unit of work and close it.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_DB_PATH = Path(os.environ.get("FORECAST_DB_PATH", str(DATA_DIR / "forecast.sqlite")))

DDL = """
CREATE TABLE IF NOT EXISTS markets (
    uid TEXT PRIMARY KEY,
    venue TEXT,
    venue_id TEXT,
    event_ticker TEXT,
    question TEXT,
    category TEXT,
    url TEXT,
    end_date TEXT,
    yes_token_id TEXT,
    no_token_id TEXT,
    active INTEGER DEFAULT 1,
    resolved INTEGER DEFAULT 0,
    outcome INTEGER,
    resolved_at TEXT,
    first_seen TEXT,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    market_uid TEXT,
    yes_bid REAL,
    yes_ask REAL,
    last_price REAL,
    baseline_prob REAL,
    spread REAL,
    liquidity REAL,
    volume_24h REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_market_ts ON snapshots(market_uid, ts);

CREATE TABLE IF NOT EXISTS model_probs (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    market_uid TEXT,
    source TEXT,
    model_prob REAL,
    prob_method TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_probs_market ON model_probs(market_uid, source, ts);

CREATE TABLE IF NOT EXISTS venue_links (
    id INTEGER PRIMARY KEY,
    poly_uid TEXT,
    kalshi_uid TEXT,
    method TEXT,
    score REAL,
    status TEXT,
    created_at TEXT,
    UNIQUE(poly_uid, kalshi_uid)
);

CREATE TABLE IF NOT EXISTS calibration (
    id INTEGER PRIMARY KEY,
    market_uid TEXT,
    platform TEXT,
    category TEXT,
    target_date TEXT,
    source TEXT DEFAULT 'market_baseline',
    model_prob REAL,
    market_prob REAL,
    outcome INTEGER,
    displayed_at TEXT,
    resolved_at TEXT,
    prob_method TEXT DEFAULT 'market',
    UNIQUE(market_uid, source, displayed_at)
);
CREATE INDEX IF NOT EXISTS idx_calibration_market ON calibration(market_uid);

CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY,
    market_uid TEXT,
    ts TEXT,
    title TEXT,
    source TEXT,
    url TEXT,
    published TEXT,
    UNIQUE(market_uid, url)
);
CREATE INDEX IF NOT EXISTS idx_news_market ON news(market_uid, ts);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: connections are handed between the event loop
    # and to_thread workers; each connection is still used by one task at a time.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # WAL+NORMAL skips the per-commit fsync; ingest writes thousands of
    # autocommitted rows per cycle and FULL made that IO-bound.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(DDL)
    conn.commit()
    return conn


def make_uid(venue: str, venue_id: str) -> str:
    return f"{venue}:{venue_id}"


# ── markets / snapshots ──────────────────────────────────────────────────────


def upsert_market(conn: sqlite3.Connection, m: dict) -> str:
    """Insert or refresh a market row. Returns the uid.

    Accepts either an explicit m['uid'] or derives it as '<venue>:<venue_id>'.
    first_seen is set once; last_seen refreshes on every call.
    """
    uid = m.get("uid") or make_uid(m["venue"], m["venue_id"])
    now = _now_iso()
    conn.execute(
        """INSERT INTO markets
               (uid, venue, venue_id, event_ticker, question, category, url,
                end_date, yes_token_id, no_token_id, active, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT(uid) DO UPDATE SET
               event_ticker = excluded.event_ticker,
               question = excluded.question,
               category = excluded.category,
               url = excluded.url,
               end_date = excluded.end_date,
               yes_token_id = excluded.yes_token_id,
               no_token_id = excluded.no_token_id,
               active = 1,
               last_seen = excluded.last_seen""",
        (uid, m.get("venue"), m.get("venue_id"), m.get("event_ticker"), m.get("question"), m.get("category"), m.get("url"), m.get("end_date"), m.get("yes_token_id"), m.get("no_token_id"), now, now),
    )
    conn.commit()
    return uid


def get_market(conn: sqlite3.Connection, uid: str) -> dict | None:
    row = conn.execute("SELECT * FROM markets WHERE uid = ?", (uid,)).fetchone()
    return dict(row) if row else None


def insert_snapshot(conn: sqlite3.Connection, s: dict) -> None:
    conn.execute(
        """INSERT INTO snapshots
               (ts, market_uid, yes_bid, yes_ask, last_price, baseline_prob,
                spread, liquidity, volume_24h)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (s.get("ts") or _now_iso(), s["market_uid"], s.get("yes_bid"), s.get("yes_ask"), s.get("last_price"), s.get("baseline_prob"), s.get("spread"), s.get("liquidity"), s.get("volume_24h")),
    )
    conn.commit()


def latest_snapshots(conn: sqlite3.Connection, horizon_days: int) -> list[dict]:
    """Latest snapshot per active, unresolved market whose end_date falls
    within the next `horizon_days`, joined with market fields. Each row gets
    a 'model_probs' dict: {source: {model_prob, prob_method, ts, detail}}
    holding the latest model prob per source.
    """
    now = _now_iso()
    cutoff = (datetime.now(timezone.utc) + timedelta(days=horizon_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT m.*, s.ts AS snapshot_ts, s.yes_bid, s.yes_ask, s.last_price,
                  s.baseline_prob, s.spread, s.liquidity, s.volume_24h
           FROM markets m
           JOIN snapshots s ON s.id = (
               SELECT id FROM snapshots
               WHERE market_uid = m.uid
               ORDER BY ts DESC, id DESC LIMIT 1
           )
           WHERE m.active = 1 AND m.resolved = 0
             AND m.end_date IS NOT NULL
             AND m.end_date >= ? AND m.end_date <= ?
           ORDER BY m.end_date ASC""",
        (now, cutoff),
    ).fetchall()
    return _attach_model_probs(conn, [dict(r) for r in rows])


def _attach_model_probs(conn: sqlite3.Connection, result: list[dict]) -> list[dict]:
    """Adds 'model_probs' ({source: {model_prob, prob_method, ts, detail}},
    latest per source) to each market-shaped row in place."""
    if not result:
        return result

    uids = [r["uid"] for r in result]
    placeholders = ",".join("?" for _ in uids)
    mp_rows = conn.execute(
        f"""SELECT mp.market_uid, mp.source, mp.model_prob, mp.prob_method,
                   mp.ts, mp.detail
            FROM model_probs mp
            WHERE mp.market_uid IN ({placeholders})
              AND mp.id = (
                  SELECT id FROM model_probs
                  WHERE market_uid = mp.market_uid AND source = mp.source
                  ORDER BY ts DESC, id DESC LIMIT 1
              )""",
        uids,
    ).fetchall()
    by_uid: dict[str, dict] = {}
    for r in mp_rows:
        by_uid.setdefault(r["market_uid"], {})[r["source"]] = {
            "model_prob": r["model_prob"],
            "prob_method": r["prob_method"],
            "ts": r["ts"],
            "detail": r["detail"],
        }
    for row in result:
        row["model_probs"] = by_uid.get(row["uid"], {})
    return result


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_SEARCH_STOPWORDS = frozenset("will the a an in on at of to be is are was were do does did it its this that by for with and or what when who how much many".split())


def search_terms(q: str) -> list[str]:
    """Whitespace-split with punctuation stripped from term edges and English
    stopwords dropped, so natural questions ('Will the Fed cut rates?') match
    market titles on their content words. If EVERY term is a stopword the
    original terms are kept — a query like 'will it' should still be a query.
    Shared by search_markets and the board's q filter."""
    raw = []
    for t in (q or "").split():
        t = t.strip(string.punctuation)
        if t:
            raw.append(t)
    content = [t for t in raw if t.lower() not in _SEARCH_STOPWORDS]
    # Trailing-s strip so 'rates' matches 'rate' (and, as a substring, 'rates').
    content = [t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t for t in content]
    return content or raw


def search_markets(conn: sqlite3.Connection, q: str, limit: int = 50) -> list[dict]:
    """Active, unresolved markets whose question contains every
    whitespace-separated term of `q` (case-insensitive AND of LIKEs, edge
    punctuation ignored), each joined with its latest snapshot and latest
    model probs (same row shape as latest_snapshots), ordered by liquidity
    desc.
    """
    terms = search_terms(q)
    if not terms:
        return []
    where_likes = " AND ".join(["m.question LIKE ? ESCAPE '\\'"] * len(terms))
    params = [f"%{_like_escape(t)}%" for t in terms]
    rows = conn.execute(
        f"""SELECT m.*, s.ts AS snapshot_ts, s.yes_bid, s.yes_ask, s.last_price,
                   s.baseline_prob, s.spread, s.liquidity, s.volume_24h
            FROM markets m
            JOIN snapshots s ON s.id = (
                SELECT id FROM snapshots
                WHERE market_uid = m.uid
                ORDER BY ts DESC, id DESC LIMIT 1
            )
            WHERE m.active = 1 AND m.resolved = 0
              AND {where_likes}
            ORDER BY s.liquidity DESC
            LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return _attach_model_probs(conn, [dict(r) for r in rows])


def create_custom_market(conn: sqlite3.Connection, question: str, end_date: str | None = None) -> str:
    """Upsert a venue='custom' market keyed by the question text (venue_id =
    sha1(question.lower())[:16], so the same question always maps to the
    same uid). Returns the uid.
    """
    venue_id = hashlib.sha1(question.lower().encode("utf-8")).hexdigest()[:16]
    uid = make_uid("custom", venue_id)
    now = _now_iso()
    # COALESCE: a re-ask without an end_date must not clear one set earlier.
    conn.execute(
        """INSERT INTO markets (uid, venue, venue_id, question, category, end_date, active, first_seen, last_seen)
           VALUES (?, 'custom', ?, ?, 'custom', ?, 1, ?, ?)
           ON CONFLICT(uid) DO UPDATE SET
               end_date = COALESCE(excluded.end_date, markets.end_date),
               active = 1,
               last_seen = excluded.last_seen""",
        (uid, venue_id, question, end_date, now, now),
    )
    conn.commit()
    return uid


def latest_snapshot(conn: sqlite3.Connection, uid: str) -> dict | None:
    row = conn.execute(
        """SELECT ts, yes_bid, yes_ask, last_price, baseline_prob, spread,
                  liquidity, volume_24h
           FROM snapshots WHERE market_uid = ?
           ORDER BY ts DESC, id DESC LIMIT 1""",
        (uid,),
    ).fetchone()
    return dict(row) if row else None


def snapshot_history(conn: sqlite3.Connection, uid: str, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """SELECT ts, yes_bid, yes_ask, last_price, baseline_prob, spread,
                  liquidity, volume_24h
           FROM snapshots WHERE market_uid = ?
           ORDER BY ts DESC, id DESC LIMIT ?""",
        (uid, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def baseline_asof(conn: sqlite3.Connection, uid: str, ts_iso: str) -> float | None:
    """baseline_prob of the most recent snapshot at or before ts_iso."""
    row = conn.execute(
        """SELECT baseline_prob FROM snapshots
           WHERE market_uid = ? AND ts <= ?
           ORDER BY ts DESC, id DESC LIMIT 1""",
        (uid, ts_iso),
    ).fetchone()
    return row["baseline_prob"] if row else None


# ── model probs ──────────────────────────────────────────────────────────────


def insert_model_prob(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT INTO model_probs (ts, market_uid, source, model_prob, prob_method, detail)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (row.get("ts") or _now_iso(), row["market_uid"], row["source"], row.get("model_prob"), row.get("prob_method"), row.get("detail")),
    )
    conn.commit()


def model_prob_history(conn: sqlite3.Connection, uid: str, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """SELECT ts, source, model_prob, prob_method, detail
           FROM model_probs WHERE market_uid = ?
           ORDER BY ts DESC, id DESC LIMIT ?""",
        (uid, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ── venue links ──────────────────────────────────────────────────────────────


def upsert_link(conn: sqlite3.Connection, poly_uid: str, kalshi_uid: str, method: str, score: float, status: str) -> None:
    # Manual verdicts (confirmed/rejected) survive re-matching; auto status
    # and scores refresh freely.
    conn.execute(
        """INSERT INTO venue_links (poly_uid, kalshi_uid, method, score, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(poly_uid, kalshi_uid) DO UPDATE SET
               method = excluded.method,
               score = excluded.score,
               status = CASE WHEN venue_links.status IN ('confirmed', 'rejected')
                             THEN venue_links.status ELSE excluded.status END""",
        (poly_uid, kalshi_uid, method, score, status, _now_iso()),
    )
    conn.commit()


def links(conn: sqlite3.Connection, status_filter: str | None = None) -> list[dict]:
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM venue_links WHERE status = ? ORDER BY score DESC",
            (status_filter,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM venue_links ORDER BY score DESC").fetchall()
    return [dict(r) for r in rows]


def set_link_status(conn: sqlite3.Connection, poly_uid: str, kalshi_uid: str, status: str) -> bool:
    cur = conn.execute(
        "UPDATE venue_links SET status = ? WHERE poly_uid = ? AND kalshi_uid = ?",
        (status, poly_uid, kalshi_uid),
    )
    conn.commit()
    return cur.rowcount > 0


# ── calibration / resolution ─────────────────────────────────────────────────


def log_calibration_row(conn: sqlite3.Connection, row: dict) -> None:
    """INSERT OR IGNORE — UNIQUE(market_uid, source, displayed_at) dedups to
    one row per market per source per displayed_at (callers pass the UTC date).
    """
    conn.execute(
        """INSERT OR IGNORE INTO calibration
               (market_uid, platform, category, target_date, source,
                model_prob, market_prob, outcome, displayed_at, resolved_at, prob_method)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["market_uid"],
            row.get("platform"),
            row.get("category"),
            row.get("target_date"),
            row.get("source", "market_baseline"),
            row.get("model_prob"),
            row.get("market_prob"),
            row.get("outcome"),
            row.get("displayed_at"),
            row.get("resolved_at"),
            row.get("prob_method", "market"),
        ),
    )
    conn.commit()


def unresolved_past_close(conn: sqlite3.Connection, grace_hours: int = 6) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=grace_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT * FROM markets
           WHERE resolved = 0 AND end_date IS NOT NULL AND end_date < ?""",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_resolved(conn: sqlite3.Connection, uid: str, outcome: int) -> None:
    conn.execute(
        "UPDATE markets SET resolved = 1, active = 0, outcome = ?, resolved_at = ? WHERE uid = ?",
        (outcome, _now_iso(), uid),
    )
    conn.commit()


def backfill_calibration_outcomes(conn: sqlite3.Connection, uid: str, outcome: int) -> int:
    cur = conn.execute(
        "UPDATE calibration SET outcome = ?, resolved_at = ? WHERE market_uid = ? AND outcome IS NULL",
        (outcome, _now_iso(), uid),
    )
    conn.commit()
    return cur.rowcount


def get_brier_score(conn: sqlite3.Connection) -> dict:
    """Brier scores over resolved calibration rows.

    brier_model / brier_market are computed head-to-head over rows where a
    model prob exists. Breakdowns score each row on its effective prob:
    model_prob when present, else market_prob (market_baseline rows).
    """
    rows = conn.execute(
        """SELECT source, platform, category, model_prob, market_prob, outcome
           FROM calibration WHERE outcome IS NOT NULL"""
    ).fetchall()
    if not rows:
        return {
            "n": 0,
            "n_model": 0,
            "brier_model": None,
            "brier_market": None,
            "edge_vs_market": None,
            "by_source": {},
            "by_platform": {},
            "by_category": {},
            "buckets": {},
        }

    n = len(rows)
    model_rows = [r for r in rows if r["model_prob"] is not None and r["market_prob"] is not None]
    brier_model = brier_market = edge = None
    if model_rows:
        nm = len(model_rows)
        brier_model = round(sum((r["model_prob"] - r["outcome"]) ** 2 for r in model_rows) / nm, 4)
        brier_market = round(sum((r["market_prob"] - r["outcome"]) ** 2 for r in model_rows) / nm, 4)
        edge = round(brier_market - brier_model, 4)

    def _effective_prob(r) -> float | None:
        return r["model_prob"] if r["model_prob"] is not None else r["market_prob"]

    def _breakdown(key: str, default: str) -> dict:
        acc: dict[str, dict] = {}
        for r in rows:
            prob = _effective_prob(r)
            if prob is None:
                continue
            label = r[key] or default
            slot = acc.setdefault(label, {"n": 0, "sum_sq": 0.0})
            slot["n"] += 1
            slot["sum_sq"] += (prob - r["outcome"]) ** 2
        return {k: {"n": v["n"], "brier": round(v["sum_sq"] / v["n"], 4)} for k, v in sorted(acc.items())}

    buckets_acc: dict[int, dict] = {}
    for r in rows:
        prob = _effective_prob(r)
        if prob is None:
            continue
        b = min(int(prob * 10), 9)
        slot = buckets_acc.setdefault(b, {"count": 0, "sum_prob": 0.0, "hits": 0})
        slot["count"] += 1
        slot["sum_prob"] += prob
        slot["hits"] += 1 if r["outcome"] else 0
    buckets = {
        f"{b * 10}-{b * 10 + 10}%": {
            "count": v["count"],
            "avg_prob": round(v["sum_prob"] / v["count"], 4),
            "hit_rate": round(v["hits"] / v["count"], 4),
        }
        for b, v in sorted(buckets_acc.items())
    }

    return {
        "n": n,
        "n_model": len(model_rows),
        "brier_model": brier_model,
        "brier_market": brier_market,
        "edge_vs_market": edge,
        "by_source": _breakdown("source", "market_baseline"),
        "by_platform": _breakdown("platform", "unknown"),
        "by_category": _breakdown("category", "uncategorized"),
        "buckets": buckets,
    }


def status_counts(conn: sqlite3.Connection) -> dict:
    def _one(sql: str, params: tuple = ()) -> int:
        return conn.execute(sql, params).fetchone()[0]

    return {
        "markets": _one("SELECT COUNT(*) FROM markets"),
        "markets_active": _one("SELECT COUNT(*) FROM markets WHERE active = 1 AND resolved = 0"),
        "markets_resolved": _one("SELECT COUNT(*) FROM markets WHERE resolved = 1"),
        "snapshots": _one("SELECT COUNT(*) FROM snapshots"),
        "model_probs": _one("SELECT COUNT(*) FROM model_probs"),
        "venue_links": _one("SELECT COUNT(*) FROM venue_links"),
        "venue_links_confirmed": _one("SELECT COUNT(*) FROM venue_links WHERE status = 'confirmed'"),
        "calibration": _one("SELECT COUNT(*) FROM calibration"),
        "calibration_resolved": _one("SELECT COUNT(*) FROM calibration WHERE outcome IS NOT NULL"),
    }


# ── baseline math ────────────────────────────────────────────────────────────


def compute_baseline(bid: float | None, ask: float | None, last: float | None, liquidity: float | None) -> dict:
    """Baseline probability from the order book.

    Both sides quoted -> midpoint, tier from spread/liquidity.
    One side quoted   -> that side, tier 'thin'.
    Neither quoted    -> last trade price, tier 'stale'.
    """
    has_bid = bid is not None and bid > 0
    has_ask = ask is not None and ask > 0
    if has_bid and has_ask:
        spread = round(ask - bid, 6)
        baseline = round((bid + ask) / 2, 6)
        liq = liquidity or 0
        if spread <= 0.02 and liq >= 1000:
            tier = "high"
        elif spread <= 0.05:
            tier = "medium"
        else:
            tier = "low"
        return {"baseline": baseline, "spread": spread, "tier": tier}
    if has_bid or has_ask:
        return {"baseline": bid if has_bid else ask, "spread": None, "tier": "thin"}
    return {"baseline": last, "spread": None, "tier": "stale"}


# ── news ─────────────────────────────────────────────────────────────────────


def insert_news(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO news (market_uid, ts, title, source, url, published)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (row["market_uid"], row.get("ts") or _now_iso(), row.get("title"), row.get("source"), row.get("url"), row.get("published")),
    )
    conn.commit()


def news_for(conn: sqlite3.Connection, uid: str, limit: int = 8) -> list[dict]:
    rows = conn.execute(
        "SELECT title, source, url, published, ts FROM news WHERE market_uid = ? ORDER BY ts DESC, id DESC LIMIT ?",
        (uid, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def resolved_calibration_pairs(conn: sqlite3.Connection, platform: str | None = None) -> list[tuple[float, int]]:
    """(market_prob, outcome) pairs from resolved market_baseline rows — the
    raw material for empirical recalibration."""
    q = "SELECT market_prob, outcome FROM calibration WHERE source = 'market_baseline' AND outcome IS NOT NULL AND market_prob IS NOT NULL"
    args: tuple = ()
    if platform:
        q += " AND platform = ?"
        args = (platform,)
    return [(float(r[0]), int(r[1])) for r in conn.execute(q, args).fetchall()]
