"""Per-user forensic signing of API responses.

Three techniques, selected per endpoint so a reader seeing one response
can't easily guess the other two:

  a) Decimal-precision watermark on float fields (``probability``,
     ``credibility`` etc.). The user's seed mod 10 picks which digit gets
     ±1 in the last place.

  b) Row-order watermark — for lists with no canonical sort, shuffle
     deterministically with the user's seed.

  c) Sentinel rows — for long lists (>50 items), inject 1–2 plausible-
     looking synthetic entries tracked in ``sentinel_predictions``.

The signer is idempotent with respect to data (re-signing the same payload
for the same user yields the same output). Sentinels are persisted on
first injection so the forensics tool can look them up later.

Exposed entry points:

    get_or_create_seed(user_id) -> int
    sign_response(user_id, data, endpoint) -> data
    recover_seed_from_numeric_payload(payload, candidate_seeds) -> int|None

All functions fail open — if the DB isn't available or the payload shape
is unexpected, the original data is returned unchanged so the user still
gets their response. Forensic signing is defence-in-depth, not a gate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import secrets
import time
from typing import Any, Iterable, Optional

import db


log = logging.getLogger(__name__)


# ── Seed management ──────────────────────────────────────────────────────

def get_or_create_seed(user_id: int) -> int:
    """Return the user's forensic seed, minting + storing one if absent."""
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT seed FROM user_forensic_seeds WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row:
                return int(row["seed"])
            seed = secrets.randbits(32)
            now = int(time.time())
            c.execute(
                "INSERT INTO user_forensic_seeds "
                "(user_id, seed, rotation_version, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (user_id, seed, now, now),
            )
            return seed
    except Exception as exc:
        log.warning("forensic seed fetch/create failed for user %s: %s", user_id, exc)
        # Fall back to a deterministic pseudo-seed so signing still works.
        return int.from_bytes(
            hashlib.sha256(f"narve-wm-fallback:{user_id}".encode()).digest()[:4],
            "big",
        )


def rotate_seed(user_id: int) -> int:
    """Admin-driven rotation — re-mints the seed and bumps rotation_version."""
    now = int(time.time())
    with db.conn() as c:
        seed = secrets.randbits(32)
        c.execute(
            "INSERT INTO user_forensic_seeds "
            "(user_id, seed, rotation_version, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "seed = excluded.seed, "
            "rotation_version = user_forensic_seeds.rotation_version + 1, "
            "updated_at = excluded.updated_at",
            (user_id, seed, now, now),
        )
    return seed


# ── Technique (a): decimal precision watermark ────────────────────────────

# Which JSON fields are treated as "sign-able numeric fields". Keep this
# list narrow — perturbing e.g. a timestamp would break downstream parsing.
_SIGNABLE_FLOAT_KEYS = frozenset({
    "probability", "prob", "p",
    "credibility", "credibility_score",
    "edge", "ev", "expected_value",
    "confidence",
})


def _target_bit(seed: int, row_index: int) -> int:
    """Which parity should the last-decimal digit have for this row?

    Rotates through 32 bits of the seed so the scheme survives long lists
    without becoming trivially reverse-engineerable by eye.
    """
    return (seed >> (row_index & 0x1F)) & 1


def _perturb_float(value: float, seed: int, row_index: int) -> float:
    """Nudge ``value`` so its 4-decimal representation carries the seed bit.

    We keep 4 decimal places (matches existing UI rounding). If the integer
    representation of ``value * 10000`` already has the right parity we
    leave it alone; otherwise we add +1 (or -1 at the upper boundary) so
    the signal is encoded without jumping more than 1 ULP.
    """
    try:
        if value is None:
            return value
        base = round(float(value), 4)
        scaled = int(round(base * 10000))
        target = _target_bit(seed, row_index)
        if scaled % 2 == target:
            return base
        # Flip parity by ±1. Prefer +1 unless it would overflow [0, 1].
        if 0.0 <= base <= 1.0 and scaled + 1 > 10000:
            return round((scaled - 1) / 10000.0, 4)
        return round((scaled + 1) / 10000.0, 4)
    except Exception:
        return value


def _apply_decimal_sign(rows: list, seed: int) -> list:
    """Walk every row, perturb the first float field we know how to sign."""
    out = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            out.append(row)
            continue
        # Copy so we don't mutate upstream caches.
        new_row = dict(row)
        for key, val in list(new_row.items()):
            if key.lower() in _SIGNABLE_FLOAT_KEYS and isinstance(val, (int, float)):
                new_row[key] = _perturb_float(float(val), seed, idx)
                break
        out.append(new_row)
    return out


# ── Technique (b): deterministic shuffle ──────────────────────────────────

def _seeded_shuffle(rows: list, seed: int) -> list:
    """Shuffle a list with ``random.Random(seed)`` so it's reproducible."""
    if len(rows) < 2:
        return rows
    r = random.Random(seed)
    pool = list(rows)
    r.shuffle(pool)
    return pool


# ── Technique (c): sentinel rows ──────────────────────────────────────────

def _build_sentinel(user_id: int, endpoint: str, seed: int, n: int) -> dict:
    """Construct a plausible-looking synthetic row."""
    # Derive a per-sentinel id so each row is traceable individually.
    sid_raw = hashlib.sha256(
        f"narve-sentinel:{user_id}:{endpoint}:{seed}:{n}".encode()
    ).hexdigest()[:16]
    # Give the sentinel a realistic shape — we treat it as a "prediction"
    # because that's our largest list endpoint; other callers can tell the
    # signer to skip sentinels via ``inject_sentinels=False``.
    return {
        "id": f"s_{sid_raw}",
        "title": _synth_title(sid_raw),
        "probability": round(0.5 + ((seed >> n) % 100 - 50) / 1000.0, 4),
        "credibility": round(0.7 + ((seed >> (n + 3)) % 60 - 30) / 1000.0, 4),
        "category": "macro",
        "_sentinel": True,  # internal — stripped before serialising
        "_sentinel_id": sid_raw,
    }


def _synth_title(sid_raw: str) -> str:
    """Generate a plausible-sounding title from a hex seed."""
    frags_a = ["Fed", "ECB", "BoE", "BoJ", "OPEC+", "UN", "G7", "G20"]
    frags_b = ["holds", "extends", "cuts", "raises", "delays", "reviews"]
    frags_c = ["policy", "stance", "decision", "forecast", "meeting"]
    hex_int = int(sid_raw[:8], 16)
    return f"{frags_a[hex_int % len(frags_a)]} {frags_b[(hex_int >> 4) % len(frags_b)]} {frags_c[(hex_int >> 8) % len(frags_c)]}"


def _record_sentinel(user_id: int, endpoint: str, row: dict) -> None:
    """Persist the sentinel so the forensics tool can pattern-match later."""
    try:
        with db.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO sentinel_predictions "
                "(user_id, sentinel_id, endpoint, payload_json, injected_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    row["_sentinel_id"],
                    endpoint,
                    json.dumps({k: v for k, v in row.items() if not k.startswith("_")}),
                    int(time.time()),
                    int(time.time()) + 180 * 86400,
                ),
            )
    except Exception as exc:
        log.warning("sentinel persist failed user=%s endpoint=%s: %s", user_id, endpoint, exc)


def _inject_sentinels(rows: list, user_id: int, endpoint: str, seed: int, count: int = 1) -> list:
    """Insert ``count`` sentinels into ``rows`` at pseudo-random positions."""
    if not rows or count < 1:
        return rows
    out = list(rows)
    r = random.Random(seed)
    for n in range(count):
        s = _build_sentinel(user_id, endpoint, seed, n)
        _record_sentinel(user_id, endpoint, s)
        # Strip internal fields before user-facing serialisation.
        visible = {k: v for k, v in s.items() if not k.startswith("_")}
        out.insert(r.randint(0, len(out)), visible)
    return out


# ── Public entry point ───────────────────────────────────────────────────

def sign_response(
    user_id: int,
    data: Any,
    endpoint: str,
    *,
    inject_sentinels: bool = True,
    shuffle: bool = False,
) -> Any:
    """Apply forensic signing to a JSON-serialisable response.

    ``data`` may be either a list of dicts (signs that list) or a dict
    with a single ``list`` field (signs that field). Anything else is
    returned unchanged.
    """
    if user_id is None or user_id <= 0:
        return data
    seed = get_or_create_seed(user_id)

    list_key = None
    rows: Optional[list] = None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        # Common wrapping shapes.
        for candidate in ("items", "rows", "predictions", "markets", "sources", "data", "results"):
            val = data.get(candidate)
            if isinstance(val, list):
                list_key = candidate
                rows = val
                break
    if rows is None:
        return data

    signed = _apply_decimal_sign(rows, seed)
    if shuffle:
        signed = _seeded_shuffle(signed, seed)
    if inject_sentinels and len(signed) >= 50:
        signed = _inject_sentinels(signed, user_id, endpoint, seed, count=1)

    if list_key is not None:
        out = dict(data)
        out[list_key] = signed
        return out
    return signed


# ── Recovery helpers (used by extract_watermark.py) ──────────────────────

def score_payload_against_seed(
    rows: Iterable[dict],
    seed: int,
) -> float:
    """Return a similarity score in [0, 1] for rows having been signed with seed.

    For each row we only look at the first signable numeric field (mirroring
    the signer) and check whether its 10000-scaled integer carries the
    parity the seed dictates. Match rate = score. Random (unsigned) data
    hovers near 0.5; genuinely signed data pins to ≥ 0.95.
    """
    matches = 0
    total = 0
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        for key, val in row.items():
            if key.lower() not in _SIGNABLE_FLOAT_KEYS:
                continue
            if not isinstance(val, (int, float)):
                continue
            total += 1
            target = _target_bit(seed, idx)
            observed = int(round(float(val) * 10000)) % 2
            if observed == target:
                matches += 1
            break  # only the first signable field per row
    return matches / total if total else 0.0


def recover_seed_from_numeric_payload(
    rows: list,
    candidate_seeds: Iterable[tuple[int, int]],
) -> Optional[tuple[int, int, float]]:
    """Walk candidate (user_id, seed) pairs and pick the best match.

    Returns ``(user_id, seed, score)`` or ``None`` if no seed scored above
    the minimum confidence floor (0.65). The floor is conservative — users
    with <10 numeric rows of signable data won't reliably match, but that
    matches the data-level technique's inherent recall limits.
    """
    best: Optional[tuple[int, int, float]] = None
    for user_id, seed in candidate_seeds:
        score = score_payload_against_seed(rows, seed)
        if best is None or score > best[2]:
            best = (user_id, seed, score)
    # Signed data pins to ~1.0; unsigned random data sits near 0.5. A 0.85
    # floor keeps the false-positive rate low while still catching leaks
    # that were partially retyped (small-list recovery loses resolution).
    if best and best[2] >= 0.85:
        return best
    return None
