"""Public track record — resolution backfiller + signed daily rollups.

Most "we have edge" claims in prediction-market signal services are
unverifiable. This module produces a chain-linked, HMAC-signed daily
ledger so anyone can verify our model's calls weren't backdated.

Pieces
------
* `resolve_signals` walks `weather_signals_log` for markets whose
  target_date has passed, fetches the observed high from Open-Meteo's
  archive, parses the question's threshold, and writes a YES/NO row
  to `weather_resolutions`. Idempotent — re-running it is safe.

* `build_daily_rollup` aggregates everything that happened on one
  UTC date: signals fired, their outcomes (when known), Brier + log
  loss, reliability buckets, paper-trading PnL. Returns a JSON-able
  dict plus the rollup's content hash.

* `commit_rollup` stores the rollup in `track_record_rollups` with
  `prev_hash` chained to yesterday's record and an HMAC-SHA256
  signature using a key derived from `.secret_key`. Returns the
  committed row.

* `verify_chain` walks the table top-to-bottom and confirms the
  chain (`prev_hash` of each row equals the hash of the row before
  it) plus the per-row HMAC. Used by the public verification page.

The key insight on tamper-evidence: as long as the most recent hash
has been published *somewhere external* (Twitter, a Git commit, a
public timestamping service), it commits to the entire history. We
take a server-side step (committing to the chain) and leave external
publication as an operator choice.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

import weather_calibration as _wcal
import weather_pure as _wpure

logger = logging.getLogger(__name__)

DOMAIN_HMAC = b"narve-track-record-hmac-v1"
GENESIS_HASH = "genesis"


def _hmac_key(secret: bytes) -> bytes:
    """Derive a separate signing key from the server secret.

    Domain-separated SHA-256 keeps the track-record HMAC distinct from
    the Fernet key used by the credential vault, so a key leak in one
    place doesn't compromise both.
    """
    return hashlib.sha256(DOMAIN_HMAC + secret).digest()


def _content_hash(payload: dict) -> str:
    """Canonical SHA-256 over the rollup payload. Uses sorted-keys JSON
    so the hash is reproducible across implementations."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _sign(payload: dict, secret: bytes) -> str:
    """HMAC-SHA256 the canonical JSON. Returned as base64."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.b64encode(hmac.new(_hmac_key(secret), blob, hashlib.sha256).digest()).decode()


def _verify(payload: dict, signature_b64: str, secret: bytes) -> bool:
    expected = _sign(payload, secret)
    return hmac.compare_digest(expected, signature_b64)


# ─── Resolution backfiller ────────────────────────────────────────────────────

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def _fetch_observed_high(lat: float, lon: float, date_iso: str,
                         timeout: int = 20) -> Optional[float]:
    try:
        resp = requests.get(OPEN_METEO_ARCHIVE, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "start_date": date_iso, "end_date": date_iso,
        }, timeout=timeout, headers={"User-Agent": "narve-track/1.0"})
        if resp.status_code != 200:
            return None
        daily = resp.json().get("daily", {})
        highs = daily.get("temperature_2m_max") or []
        if highs and highs[0] is not None:
            return float(highs[0])
    except requests.RequestException as e:
        logger.debug("open-meteo archive fetch failed: %s", e)
    return None


def resolve_signals(conn_factory, station_coords: dict,
                    max_per_pass: int = 200) -> dict:
    """Walk un-resolved signals and write outcomes to weather_resolutions.

    `station_coords` is the same shape as server.py STATION_MAP but only
    needs (lat, lon) — we pass it in so this module doesn't import
    server. Caps at `max_per_pass` rows per call to stay polite to
    Open-Meteo.
    """
    stats = {"checked": 0, "resolved": 0, "skipped_no_threshold": 0,
             "skipped_no_observed": 0, "skipped_no_city": 0}

    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT s.market_id, s.question, s.timestamp
               FROM weather_signals_log s
               LEFT JOIN weather_resolutions r ON r.market_id = s.market_id
               WHERE r.market_id IS NULL
               GROUP BY s.market_id
               ORDER BY s.timestamp ASC LIMIT ?""",
            (int(max_per_pass),),
        ).fetchall()
    if not rows:
        return stats

    # Pull each market's target_date via the matching weather_price_snapshots
    # row (which is where the city + target_date are stored). The signals
    # table itself only has the question text.
    with conn_factory(readonly=True) as conn:
        snap_rows = conn.execute(
            """SELECT market_id, city, target_date
               FROM weather_price_snapshots
               WHERE market_id IN ({}) GROUP BY market_id""".format(
                ",".join("?" for _ in rows)
            ),
            tuple(r["market_id"] for r in rows),
        ).fetchall()
    snap_lookup = {r["market_id"]: (r["city"], r["target_date"]) for r in snap_rows}

    for r in rows:
        stats["checked"] += 1
        market_id = r["market_id"]
        question = r["question"] or ""
        city, target_date = snap_lookup.get(market_id, (None, None))
        if not city or not target_date:
            stats["skipped_no_city"] += 1
            continue
        coords = station_coords.get(city)
        if not coords:
            stats["skipped_no_city"] += 1
            continue
        try:
            target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            stats["skipped_no_city"] += 1
            continue
        # Don't try to resolve before the day has ended
        if target_dt.date() >= datetime.now(timezone.utc).date():
            continue
        threshold = _wpure.parse_threshold_for_resolution(question)
        if threshold[0] is None:
            stats["skipped_no_threshold"] += 1
            continue
        obs = _fetch_observed_high(coords[0], coords[1], target_date)
        if obs is None:
            stats["skipped_no_observed"] += 1
            continue
        yes_wins = _wpure.resolve_market(obs, threshold)
        if yes_wins is None:
            stats["skipped_no_threshold"] += 1
            continue
        outcome = "YES" if yes_wins else "NO"
        with conn_factory() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO weather_resolutions
                       (market_id, actual_outcome, payout, resolved_at)
                   VALUES (?, ?, ?, ?)""",
                (market_id, outcome, 1.0 if yes_wins else 0.0,
                 datetime.now(timezone.utc).isoformat()),
            )
        stats["resolved"] += 1
    return stats


# ─── Daily rollup builder ─────────────────────────────────────────────────────

@dataclass
class DailyRollup:
    """Returned by `build_daily_rollup` — the public-facing record."""
    date: str
    payload: dict
    content_hash: str


def build_daily_rollup(conn_factory, date_iso: str) -> DailyRollup:
    """Aggregate every signal fired on `date_iso` (UTC) with its outcome.

    The payload is deliberately small (no raw signal rows) so that
    the daily JSON is human-skimmable. The reliability bins use the
    same logic as `/api/calibration` so the public page and the
    private endpoint always agree.
    """
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT s.market_id, s.question, s.model_prob, s.edge,
                      s.yes_price, s.category, r.actual_outcome
               FROM weather_signals_log s
               LEFT JOIN weather_resolutions r ON r.market_id = s.market_id
               WHERE substr(s.timestamp, 1, 10) = ?""",
            (date_iso,),
        ).fetchall()

    signals = []
    preds, outcomes = [], []
    for r in rows:
        d = dict(r)
        # Only include rows where the model actually had a probability;
        # rows without one don't tell us about calibration.
        if d.get("model_prob") is None:
            continue
        signals.append(d)
        if d.get("actual_outcome") in ("YES", "NO"):
            preds.append(float(d["model_prob"]))
            outcomes.append(1 if d["actual_outcome"] == "YES" else 0)

    n_total = len(signals)
    n_resolved = len(preds)
    brier = round(_wcal.brier_score(preds, outcomes) or 0.0, 5) if preds else None
    log_loss = round(_wcal.log_loss(preds, outcomes) or 0.0, 5) if preds else None
    reliability = _wcal.reliability_diagram(preds, outcomes, n_bins=10) if preds else []

    # Win rate of "trade the sign of edge" — same as the backtest's bet rule.
    n_correct = 0
    for s in signals:
        if s.get("actual_outcome") not in ("YES", "NO"):
            continue
        edge = float(s.get("edge") or 0)
        won = (edge > 0 and s["actual_outcome"] == "YES") or (edge < 0 and s["actual_outcome"] == "NO")
        if won:
            n_correct += 1
    win_rate = round(n_correct / n_resolved, 4) if n_resolved else None

    payload = {
        "date": date_iso,
        "version": 1,
        "n_signals": n_total,
        "n_resolved": n_resolved,
        "win_rate": win_rate,
        "brier_score": brier,
        "log_loss": log_loss,
        "reliability": reliability,
        "categories": _category_breakdown(signals),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return DailyRollup(date=date_iso, payload=payload,
                       content_hash=_content_hash(payload))


def _category_breakdown(signals: list[dict]) -> dict:
    """Per-category counts, resolved counts, and win rates."""
    by_cat: dict = {}
    for s in signals:
        cat = s.get("category") or "other"
        bucket = by_cat.setdefault(cat, {"n_total": 0, "n_resolved": 0, "n_correct": 0})
        bucket["n_total"] += 1
        if s.get("actual_outcome") in ("YES", "NO"):
            bucket["n_resolved"] += 1
            edge = float(s.get("edge") or 0)
            if (edge > 0 and s["actual_outcome"] == "YES") or (edge < 0 and s["actual_outcome"] == "NO"):
                bucket["n_correct"] += 1
    for b in by_cat.values():
        b["win_rate"] = (round(b["n_correct"] / b["n_resolved"], 4)
                          if b["n_resolved"] else None)
    return by_cat


# ─── Chain commitment ─────────────────────────────────────────────────────────

def _latest_committed_hash(conn_factory) -> str:
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            "SELECT content_hash FROM track_record_rollups ORDER BY date DESC LIMIT 1"
        ).fetchone()
    return row["content_hash"] if row else GENESIS_HASH


def commit_rollup(conn_factory, rollup: DailyRollup, secret: bytes) -> dict:
    """Persist a rollup as the latest link in the chain.

    Refuses to commit if a row for the same date already exists — call
    sites that need to refresh a day should explicitly delete that row
    first (with an audit trail) rather than silently rewriting history.
    """
    with conn_factory(readonly=True) as conn:
        existing = conn.execute(
            "SELECT 1 FROM track_record_rollups WHERE date = ?",
            (rollup.date,),
        ).fetchone()
    if existing:
        raise ValueError(f"rollup for {rollup.date} already committed")

    prev_hash = _latest_committed_hash(conn_factory)
    # The signed envelope includes prev_hash so the chain is part of
    # the HMAC. Editing yesterday would invalidate today's signature.
    envelope = {
        "date": rollup.date,
        "payload": rollup.payload,
        "content_hash": rollup.content_hash,
        "prev_hash": prev_hash,
    }
    signature = _sign(envelope, secret)
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn_factory() as conn:
        conn.execute(
            """INSERT INTO track_record_rollups
                   (date, payload, content_hash, prev_hash, hmac_sig, committed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rollup.date,
             json.dumps(rollup.payload, sort_keys=True, separators=(",", ":")),
             rollup.content_hash, prev_hash, signature, now_iso),
        )
    return {"date": rollup.date, "content_hash": rollup.content_hash,
            "prev_hash": prev_hash, "hmac_sig": signature,
            "committed_at": now_iso}


def list_rollups(conn_factory, limit: int = 365) -> list[dict]:
    """Public-facing manifest of all committed rollups (hashes only, no
    payload bodies — those are fetched by date on demand)."""
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT date, content_hash, prev_hash, hmac_sig, committed_at
               FROM track_record_rollups
               ORDER BY date DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_rollup(conn_factory, date_iso: str) -> Optional[dict]:
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT date, payload, content_hash, prev_hash, hmac_sig, committed_at
               FROM track_record_rollups WHERE date = ?""",
            (date_iso,),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["payload"] = json.loads(out["payload"])
    except (TypeError, ValueError):
        pass
    return out


def verify_chain(conn_factory, secret: bytes) -> dict:
    """Walk the full chain top-to-bottom and validate every link.

    Returns ``{ok, n_rows, first_bad_date, errors}``. The first
    discrepancy stops the walk so the operator sees exactly which day
    broke the chain.
    """
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT date, payload, content_hash, prev_hash, hmac_sig
               FROM track_record_rollups ORDER BY date ASC"""
        ).fetchall()
    errors: list[dict] = []
    expected_prev = GENESIS_HASH
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except (TypeError, ValueError):
            errors.append({"date": r["date"], "error": "payload_not_json"})
            break
        recomputed = _content_hash(payload)
        if recomputed != r["content_hash"]:
            errors.append({"date": r["date"], "error": "content_hash_mismatch",
                           "expected": recomputed, "stored": r["content_hash"]})
            break
        if r["prev_hash"] != expected_prev:
            errors.append({"date": r["date"], "error": "prev_hash_mismatch",
                           "expected": expected_prev, "stored": r["prev_hash"]})
            break
        envelope = {
            "date": r["date"], "payload": payload,
            "content_hash": r["content_hash"], "prev_hash": r["prev_hash"],
        }
        if not _verify(envelope, r["hmac_sig"], secret):
            errors.append({"date": r["date"], "error": "hmac_mismatch"})
            break
        expected_prev = r["content_hash"]
    return {
        "ok": not errors,
        "n_rows": len(rows),
        "errors": errors,
        "first_bad_date": errors[0]["date"] if errors else None,
        "latest_hash": expected_prev,
    }


# ─── Lifetime aggregates (no date filter) ─────────────────────────────────────

def lifetime_summary(conn_factory) -> dict:
    """All-time aggregates for the front page of the track record."""
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT s.model_prob, s.edge, r.actual_outcome
               FROM weather_signals_log s
               LEFT JOIN weather_resolutions r ON r.market_id = s.market_id
               WHERE s.model_prob IS NOT NULL"""
        ).fetchall()
    n_total = len(rows)
    preds, outcomes = [], []
    n_correct = 0
    n_resolved = 0
    for r in rows:
        if r["actual_outcome"] not in ("YES", "NO"):
            continue
        n_resolved += 1
        preds.append(float(r["model_prob"]))
        outcomes.append(1 if r["actual_outcome"] == "YES" else 0)
        edge = float(r["edge"] or 0)
        if (edge > 0 and r["actual_outcome"] == "YES") or (edge < 0 and r["actual_outcome"] == "NO"):
            n_correct += 1
    return {
        "n_total": n_total,
        "n_resolved": n_resolved,
        "win_rate": round(n_correct / n_resolved, 4) if n_resolved else None,
        "brier_score": round(_wcal.brier_score(preds, outcomes) or 0.0, 5) if preds else None,
        "log_loss": round(_wcal.log_loss(preds, outcomes) or 0.0, 5) if preds else None,
        "reliability": _wcal.reliability_diagram(preds, outcomes, n_bins=10) if preds else [],
    }
