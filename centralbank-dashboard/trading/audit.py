"""Append-only audit log for every trading action.

Why a separate audit log (not just request access logs):
  Trading is the one place where a silent regression is genuinely dangerous —
  a stale price, a wrong-direction sign, a swallowed error can cost the user
  real money. The audit log is the single, write-only record of *what we
  actually sent and what Kalshi actually said*. Operators can reconcile
  against Kalshi's own statements at month-end; users can scroll back in the
  UI to confirm what they did.

Format:
  JSONL (one JSON object per line) at ``data/audit.jsonl``. Append-only —
  never read-modify-write — so an interrupted write only ever truncates the
  most recent line. ``user_id`` is always present so per-user queries are a
  trivial filter.

Rotation:
  When the file passes ``ROTATE_BYTES`` (10 MB) the writer renames it to
  ``audit.jsonl.1`` (overwriting any previous .1) before opening a fresh
  file. We keep one prior generation so a recent reconciliation still works
  after rotation; long-term archival is the operator's job.

What gets logged:
  * ``action="key.upsert"``      — user added/replaced their Kalshi credentials
  * ``action="key.delete"``      — user removed credentials
  * ``action="mode.set"``        — paper ↔ prod toggle
  * ``action="balance.read"``    — GET /portfolio/balance
  * ``action="positions.read"``  — GET /portfolio/positions
  * ``action="orders.list"``     — GET /portfolio/orders
  * ``action="order.place"``     — POST /portfolio/orders (the big one)
  * ``action="order.cancel"``    — DELETE /portfolio/orders/{id}

For order placements we log both the request payload (sans signature) AND
the Kalshi response — so we can always answer "what was this order, and
what did Kalshi tell us?" after the fact.

Sensitive fields (private key, API key id) are NEVER written here. We use
an allowlist of fields per action — anything not on the list becomes
"<redacted>". Substring matching on field names was the previous design;
that proved too fragile as new endpoints add new field names.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_AUDIT_PATH = Path(__file__).resolve().parent.parent / "data" / "audit.jsonl"
ROTATE_BYTES = 10 * 1024 * 1024
TAIL_READ_BYTES = 64 * 1024

_lock = Lock()


# H9: allowlist of fields we actively WANT to log per action. Anything else
# in a request/response dict is replaced with "<redacted>". This is safer
# than the prior substring-blocklist because it fails closed for any new
# field that might leak credentials or session material.
_REQUEST_ALLOW = {
    "key.upsert": {"mode"},
    "key.delete": set(),
    "mode.set": {"mode"},
    "balance.read": set(),
    "positions.read": set(),
    "orders.list": set(),
    "order.place": {
        "ticker",
        "side",
        "action",
        "count",
        "type",
        "yes_price",
        "no_price",
        "time_in_force",
        "client_order_id",
    },
    "order.cancel": {"order_id"},
}

_RESPONSE_ALLOW = {
    "key.upsert": set(),
    "key.delete": set(),
    "mode.set": set(),
    # Balance/positions responses are mostly numeric — surface enough that
    # operators can reconcile, but redact anything we don't expect.
    "balance.read": {"balance", "payout", "as_of"},
    "positions.read": {"market_positions", "event_positions", "as_of"},
    "orders.list": {"orders", "cursor"},
    "order.place": {"order_id", "status", "client_order_id", "ticker", "side", "action", "count", "yes_price", "no_price", "filled_quantity"},
    "order.cancel": {"order_id", "status"},
}


def _redact_dict(d: dict, allow: set, *, depth: int = 0, max_depth: int = 8) -> dict:
    """Walk ``d`` and replace any non-allowlisted top-level field with
    "<redacted>". For nested dicts/lists the same allow set applies — Kalshi
    nests responses (e.g. {"order": {...}}) but keeps field names consistent.
    M5: cap recursion at ``max_depth`` to bound work on pathological inputs."""
    if depth > max_depth:
        return {"_truncated": "max_depth"}
    out = {}
    for k, v in d.items():
        if k in allow or k == "_raw":
            out[k] = _redact_value(v, allow, depth=depth + 1, max_depth=max_depth)
        else:
            out[k] = "<redacted>"
    return out


def _redact_value(v: Any, allow: set, *, depth: int = 0, max_depth: int = 8) -> Any:
    if depth > max_depth:
        return "<truncated>"
    if isinstance(v, dict):
        return _redact_dict(v, allow, depth=depth, max_depth=max_depth)
    if isinstance(v, list):
        return [_redact_value(x, allow, depth=depth + 1, max_depth=max_depth) for x in v[:200]]
    return v


def _redact_payload(payload: Any, action: str, kind: str) -> Any:
    """Apply the per-action allowlist. ``kind`` is "request" or "response"."""
    if not isinstance(payload, dict):
        # Lists / scalars at the top level are unusual; preserve them as-is
        # (they don't carry secrets in practice) but bound list size.
        if isinstance(payload, list):
            return payload[:200]
        return payload
    table = _REQUEST_ALLOW if kind == "request" else _RESPONSE_ALLOW
    allow = table.get(action)
    if allow is None:
        # Unknown action: redact everything. Forces a fix when adding new actions.
        return {"_unknown_action_redacted": True}
    return _redact_dict(payload, allow)


def _maybe_rotate(audit_path: Path) -> None:
    """If the file is over ROTATE_BYTES, rename to .1 (replacing any prior
    .1) before the next write. Caller already holds ``_lock``."""
    try:
        size = audit_path.stat().st_size
    except OSError:
        return
    if size < ROTATE_BYTES:
        return
    backup = audit_path.with_suffix(audit_path.suffix + ".1")
    try:
        if backup.exists():
            backup.unlink()
        audit_path.rename(backup)
    except OSError as exc:
        log.warning("audit rotation failed: %s", exc)


def write_event(
    user_id: str,
    action: str,
    *,
    ok: bool,
    request: dict | None = None,
    response: dict | None = None,
    error: str | None = None,
    mode: str | None = None,
    audit_path: Path = DEFAULT_AUDIT_PATH,
) -> None:
    """Append a single audit event. Raises OSError on disk failure — callers
    in :mod:`order_manager` catch it and surface an ``audit_warning`` to the
    user so they know to reconcile manually. (H8)"""
    record = {
        "ts": int(time.time() * 1000),  # ms — same precision as Kalshi headers
        "user_id": user_id,
        "action": action,
        "ok": ok,
        "mode": mode,
    }
    if request is not None:
        record["request"] = _redact_payload(request, action, "request")
    if response is not None:
        record["response"] = _redact_payload(response, action, "response")
    if error is not None:
        # Truncate stupendously long error strings (some HTTP libs quote whole HTML pages).
        record["error"] = str(error)[:2000]

    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        _maybe_rotate(audit_path)
        with open(audit_path, "ab") as f:
            f.write(line.encode("utf-8"))


def tail_for_user(user_id: str, limit: int = 100, audit_path: Path = DEFAULT_AUDIT_PATH) -> list[dict]:
    """Return the last ``limit`` events for ``user_id``, newest first.

    M4: read from the end of the file in TAIL_READ_BYTES chunks rather than
    walking from the start. The audit log can grow into the megabytes;
    walking it all on every UI refresh was O(file_size). Reading from the
    end keeps tail reads bounded by ``limit`` (modulo many users sharing a
    file — chunk extends until ``limit`` matches accumulate)."""
    if not audit_path.exists():
        return []
    matches: list[dict] = []
    try:
        with open(audit_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            offset = file_size
            chunk_acc = b""
            while offset > 0 and len(matches) < limit:
                read_size = min(TAIL_READ_BYTES, offset)
                offset -= read_size
                f.seek(offset)
                chunk = f.read(read_size) + chunk_acc
                # Avoid splitting a JSON line: keep everything before the
                # first newline as the leftover for the next iteration
                # (unless we're at the start of file).
                if offset > 0:
                    nl = chunk.find(b"\n")
                    if nl == -1:
                        chunk_acc = chunk
                        continue
                    chunk_acc = chunk[:nl]
                    chunk = chunk[nl + 1 :]
                else:
                    chunk_acc = b""
                # Walk the lines backwards so newest first is preserved.
                for line in reversed(chunk.splitlines()):
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("user_id") == user_id:
                        matches.append(rec)
                        if len(matches) >= limit:
                            break
    except OSError:
        return []
    return matches[:limit]


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "audit.jsonl"
        write_event("u1", "key.upsert", ok=True, mode="paper", audit_path=p)
        write_event(
            "u1",
            "order.place",
            ok=True,
            mode="paper",
            request={"ticker": "KXFEDDECISION-26JUN-C25", "side": "yes", "count": 5, "yes_price": 22, "private_key_pem": "shouldnt-leak", "api_key_id": "shouldnt-leak"},
            response={"order_id": "ord-abc", "status": "resting", "secret": "shouldnt-leak"},
            audit_path=p,
        )
        write_event("u2", "order.place", ok=False, error="insufficient balance", audit_path=p)
        events = tail_for_user("u1", audit_path=p)
        assert len(events) == 2
        assert events[0]["action"] == "order.place"  # newest first
        assert events[0]["request"].get("ticker") == "KXFEDDECISION-26JUN-C25"
        assert events[0]["request"].get("private_key_pem") == "<redacted>"
        assert events[0]["request"].get("api_key_id") == "<redacted>"
        assert events[0]["response"].get("order_id") == "ord-abc"
        assert events[0]["response"].get("secret") == "<redacted>"
        print(f"✓ {len(events)} events for u1; sensitive fields redacted via allowlist")
        print("audit log self-test passed")
