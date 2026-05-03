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

Sensitive fields (private key, API key id) are NEVER written here.
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
_lock = Lock()


def _redact(payload: Any) -> Any:
    """Strip anything that looks like a secret out of a payload before logging."""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            kl = k.lower()
            if any(s in kl for s in ("private", "secret", "password", "signature", "api_key_id", "api-key", "private_key_pem")):
                out[k] = "<redacted>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(payload, list):
        return [_redact(v) for v in payload]
    return payload


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
    """Append a single audit event. Failures here are swallowed so an audit
    write can never break the user-facing flow — but they're logged loudly
    so an operator notices the disk is full or perms broke."""
    record = {
        "ts": int(time.time() * 1000),     # ms — same precision as Kalshi headers
        "user_id": user_id,
        "action": action,
        "ok": ok,
        "mode": mode,
    }
    if request is not None:
        record["request"] = _redact(request)
    if response is not None:
        record["response"] = _redact(response)
    if error is not None:
        # Truncate stupendously long error strings (some HTTP libs quote whole HTML pages).
        record["error"] = str(error)[:2000]

    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, open(audit_path, "ab") as f:
            f.write(line.encode("utf-8"))
    except OSError as exc:
        log.error("audit.write failed for %s/%s: %s", user_id, action, exc)


def tail_for_user(user_id: str, limit: int = 100, audit_path: Path = DEFAULT_AUDIT_PATH) -> list[dict]:
    """Return the last ``limit`` events for ``user_id``. O(file size) but the
    audit log is small (one line per trading action) so tail-reads stay fast.
    For multi-GB logs we'd add an offset index — out of scope for v0.8."""
    if not audit_path.exists():
        return []
    out: list[dict] = []
    try:
        with open(audit_path, "rb") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("user_id") == user_id:
                    out.append(rec)
    except OSError:
        return []
    # Newest first
    out.reverse()
    return out[:limit]


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "audit.jsonl"
        write_event("u1", "key.upsert", ok=True, mode="paper", audit_path=p)
        write_event(
            "u1", "order.place",
            ok=True, mode="paper",
            request={"ticker": "KXFEDDECISION-26JUN-C25", "side": "yes", "count": 5, "price": 22, "private_key_pem": "shouldnt-leak"},
            response={"order_id": "ord-abc", "status": "resting"},
            audit_path=p,
        )
        write_event("u2", "order.place", ok=False, error="insufficient balance", audit_path=p)
        events = tail_for_user("u1", audit_path=p)
        assert len(events) == 2
        assert events[0]["action"] == "order.place"  # newest first
        assert events[0]["request"]["private_key_pem"] == "<redacted>"
        print(f"✓ {len(events)} events for u1; private_key redacted")
        print("audit log self-test passed")
