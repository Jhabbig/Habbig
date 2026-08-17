"""Strict CSV/JSON ingest for the three kinds: predictions, markets, resolutions.

ALL row errors are reported at once as [{line, reason}] with 1-based data
lines; valid rows are still applied (partial ingest — the file's counts land
in ingest_log). Idempotent: UNIQUE keys + INSERT OR IGNORE => dedup_skipped;
an identical file (sha256 over kind + raw bytes) short-circuits with
already_ingested: true. XLSX is OUT of v0.5 — CSV and JSON arrays only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from datetime import datetime

from . import credibility
from .db import utcnow

KINDS = ("predictions", "markets", "resolutions")

# kind -> (required columns, optional columns) — CSV header order below.
COLUMNS: dict[str, tuple[list[str], list[str]]] = {
    "predictions": (["source_id", "question_id", "predicted_probability", "stated_at"], ["note"]),
    "markets": (["venue", "market_id", "question_id", "yes_price", "captured_at"], ["liquidity"]),
    "resolutions": (["question_id", "outcome", "resolved_at"], []),
}

HEADERS: dict[str, list[str]] = {
    "predictions": ["source_id", "question_id", "predicted_probability", "stated_at", "note"],
    "markets": ["venue", "market_id", "question_id", "yes_price", "liquidity", "captured_at"],
    "resolutions": ["question_id", "outcome", "resolved_at"],
}

TEMPLATE_EXAMPLES: dict[str, list[str]] = {
    "predictions": ["race_model", "midterm-house-gop-2026", "0.62", "2026-08-01T12:00:00Z", "optional note"],
    "markets": ["polymarket", "midterm-house-gop-2026", "midterm-house-gop-2026", "0.55", "250000", "2026-08-01T12:00:00Z"],
    "resolutions": ["midterm-house-gop-2026", "yes", "2026-11-04T04:00:00Z"],
}


class IngestError(Exception):
    """File-level failure (bad kind, undecodable file, bad header/body shape)."""


def template_csv(kind: str) -> str:
    if kind not in KINDS:
        raise IngestError(f"unknown ingest kind {kind!r}")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(HEADERS[kind])
    w.writerow(TEMPLATE_EXAMPLES[kind])
    return out.getvalue()


def _s(row: dict, key: str) -> str:
    v = row.get(key)
    return "" if v is None else str(v).strip()


def _iso_ok(v: str) -> bool:
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _num(row: dict, key: str) -> tuple[float | None, str | None]:
    v = row.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, f"{key} is required"
    try:
        return float(v), None
    except (TypeError, ValueError):
        return None, f"{key} must be a number, got {str(v)!r}"


def _unknown_keys(row: dict, kind: str) -> str | None:
    allowed = set(COLUMNS[kind][0]) | set(COLUMNS[kind][1])
    unknown = sorted(k for k in row if k not in allowed and k is not None)
    if None in row:
        return "row has more cells than the header"
    if unknown:
        return f"unknown field(s): {', '.join(str(k) for k in unknown)}"
    return None


# --- per-row validators: row dict -> (clean dict | None, reason | None) ------

def _v_prediction(row: dict) -> tuple[dict | None, str | None]:
    for key in ("source_id", "question_id"):
        if not _s(row, key):
            return None, f"{key} is required"
    p, err = _num(row, "predicted_probability")
    if err:
        return None, err
    if not 0.0 <= p <= 1.0:
        return None, f"predicted_probability must be between 0 and 1, got {p}"
    stated_at = _s(row, "stated_at")
    if not stated_at:
        return None, "stated_at is required"
    if not _iso_ok(stated_at):
        return None, f"stated_at must be an ISO 8601 timestamp, got {stated_at!r}"
    note = _s(row, "note") or None
    return {
        "source_id": _s(row, "source_id"), "question_id": _s(row, "question_id"),
        "p": p, "stated_at": stated_at, "note": note,
    }, None


def _v_market(row: dict) -> tuple[dict | None, str | None]:
    for key in ("venue", "market_id", "question_id"):
        if not _s(row, key):
            return None, f"{key} is required"
    yes_price, err = _num(row, "yes_price")
    if err:
        return None, err
    if not 0.0 <= yes_price <= 1.0:
        return None, f"yes_price must be between 0 and 1, got {yes_price}"
    liquidity: float | None = None
    if _s(row, "liquidity"):
        liquidity, err = _num(row, "liquidity")
        if err:
            return None, err
        if liquidity < 0:
            return None, f"liquidity must be >= 0, got {liquidity}"
    captured_at = _s(row, "captured_at")
    if not captured_at:
        return None, "captured_at is required"
    if not _iso_ok(captured_at):
        return None, f"captured_at must be an ISO 8601 timestamp, got {captured_at!r}"
    return {
        "venue": _s(row, "venue"), "market_id": _s(row, "market_id"),
        "question_id": _s(row, "question_id"), "yes_price": yes_price,
        "liquidity": liquidity, "captured_at": captured_at,
    }, None


def _v_resolution(row: dict) -> tuple[dict | None, str | None]:
    if not _s(row, "question_id"):
        return None, "question_id is required"
    outcome = _s(row, "outcome").lower()
    if outcome not in ("yes", "no", "void"):
        return None, f"outcome must be one of yes|no|void, got {_s(row, 'outcome')!r}"
    resolved_at = _s(row, "resolved_at")
    if not resolved_at:
        return None, "resolved_at is required"
    if not _iso_ok(resolved_at):
        return None, f"resolved_at must be an ISO 8601 timestamp, got {resolved_at!r}"
    return {
        "question_id": _s(row, "question_id"), "outcome": outcome,
        "resolved_at": resolved_at,
    }, None


VALIDATORS = {
    "predictions": _v_prediction,
    "markets": _v_market,
    "resolutions": _v_resolution,
}


# --- appliers: (conn, [(line, clean)], errors) -> (ok, dedup_skipped) --------

def _ensure_source(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sources(id, name, alpha, beta, is_sample, created_at)"
        " VALUES (?, '', 2.0, 2.0, 0, ?)",
        (source_id, utcnow()),
    )


def _ensure_question(conn: sqlite3.Connection, question_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO questions(id, title, status, is_sample, created_at)"
        " VALUES (?, ?, 'live', 0, ?)",
        (question_id, question_id, utcnow()),
    )


def _apply_predictions(conn, valid, errors) -> tuple[int, int]:
    ok = dedup = 0
    for _line, r in valid:
        _ensure_source(conn, r["source_id"])
        _ensure_question(conn, r["question_id"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO predictions(source_id, question_id, p, stated_at, note)"
            " VALUES (?, ?, ?, ?, ?)",
            (r["source_id"], r["question_id"], r["p"], r["stated_at"], r["note"]),
        )
        ok, dedup = (ok + 1, dedup) if cur.rowcount else (ok, dedup + 1)
    return ok, dedup


def _apply_markets(conn, valid, errors) -> tuple[int, int]:
    ok = dedup = 0
    for _line, r in valid:
        _ensure_question(conn, r["question_id"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO market_snapshots"
            "(venue, market_id, question_id, yes_price, liquidity, captured_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (r["venue"], r["market_id"], r["question_id"], r["yes_price"],
             r["liquidity"], r["captured_at"]),
        )
        ok, dedup = (ok + 1, dedup) if cur.rowcount else (ok, dedup + 1)
    return ok, dedup


def _apply_resolutions(conn, valid, errors) -> tuple[int, int]:
    ok = 0
    for line, r in valid:
        try:
            credibility.resolve_question(
                conn, r["question_id"], r["outcome"], r["resolved_at"]
            )
            ok += 1
        except credibility.QuestionNotFound:
            errors.append({"line": line,
                           "reason": f"unknown question_id {r['question_id']!r}"})
        except credibility.AlreadyResolved:
            errors.append({"line": line,
                           "reason": f"question {r['question_id']!r} is already resolved"})
    return ok, 0


APPLIERS = {
    "predictions": _apply_predictions,
    "markets": _apply_markets,
    "resolutions": _apply_resolutions,
}


# --- entry points ------------------------------------------------------------

def parse_csv(kind: str, data: bytes) -> list[dict]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise IngestError(
            "file is not valid UTF-8 text — XLSX is not supported in v0.5, "
            "export as CSV first"
        ) from None
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    required, optional = COLUMNS[kind]
    missing = [c for c in required if c not in fields]
    if missing:
        raise IngestError(f"missing required column(s): {', '.join(missing)}")
    unknown = [c for c in fields if c not in required + optional]
    if unknown:
        raise IngestError(f"unknown column(s): {', '.join(unknown)}")
    return [dict(r) for r in reader]


def _sha(kind: str, data: bytes) -> str:
    return hashlib.sha256(kind.encode() + b"\x00" + data).hexdigest()


def _run(conn: sqlite3.Connection, kind: str, rows: list, sha: str,
         file_name: str | None) -> dict:
    prior = conn.execute(
        "SELECT rows_ok, rows_err FROM ingest_log WHERE sha256 = ?", (sha,)
    ).fetchone()
    if prior is not None:
        return {"ok_rows": prior["rows_ok"], "err_rows": prior["rows_err"],
                "dedup_skipped": 0, "already_ingested": True, "errors": []}
    errors: list[dict] = []
    valid: list[tuple[int, dict]] = []
    for line, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            errors.append({"line": line, "reason": "row must be a JSON object"})
            continue
        reason = _unknown_keys(row, kind)
        if reason is None:
            clean, reason = VALIDATORS[kind](row)
        if reason is not None:
            errors.append({"line": line, "reason": reason})
        else:
            valid.append((line, clean))
    ok, dedup = APPLIERS[kind](conn, valid, errors)
    errors.sort(key=lambda e: e["line"])
    conn.execute(
        "INSERT INTO ingest_log(file_name, kind, rows_ok, rows_err, sha256, uploaded_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (file_name, kind, ok, len(errors), sha, utcnow()),
    )
    conn.commit()
    return {"ok_rows": ok, "err_rows": len(errors), "dedup_skipped": dedup,
            "already_ingested": False, "errors": errors}


def ingest_file(conn: sqlite3.Connection, kind: str, data: bytes,
                file_name: str | None) -> dict:
    """Multipart path: raw CSV bytes (or a JSON array file)."""
    if kind not in KINDS:
        raise IngestError(f"unknown ingest kind {kind!r}")
    stripped = data.lstrip()
    if stripped.startswith(b"["):  # a .json file dropped on the upload card
        try:
            rows = json.loads(data)
        except json.JSONDecodeError as exc:
            raise IngestError(f"file looks like JSON but does not parse: {exc}") from None
        if not isinstance(rows, list):
            raise IngestError("JSON file must be an array of row objects")
    else:
        rows = parse_csv(kind, data)
    return _run(conn, kind, rows, _sha(kind, data), file_name)


def ingest_rows(conn: sqlite3.Connection, kind: str, rows: list,
                file_name: str | None = None) -> dict:
    """JSON body path: {rows: [...]} already parsed by the API layer."""
    if kind not in KINDS:
        raise IngestError(f"unknown ingest kind {kind!r}")
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return _run(conn, kind, rows, _sha(kind, canonical), file_name)
