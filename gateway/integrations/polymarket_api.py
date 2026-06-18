"""Thin Polymarket network layer. Python urllib is 403-blocked by Polymarket;
curl with a browser UA works, so every call shells out to curl. Pure I/O — no
business logic here (that lives in polymarket_ingest.py, unit-tested)."""
from __future__ import annotations
import json, subprocess, sys, time
from typing import Any, Optional

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
_UA = "Mozilla/5.0"

def _log(msg: str) -> None:
    print(f"[polymarket_api] {msg}", file=sys.stderr)

def _get(url: str, timeout: int = 20, retries: int = 1) -> Any:
    """GET via curl (urllib is 403-blocked). Returns parsed JSON, or None on a
    genuinely empty/invalid body. A curl FAILURE (non-zero exit: timeout, DNS,
    connection) is retried once, then logged + returned as None — so callers can
    at least see truncation in the logs rather than silently treating a network
    blip as 'no more data'."""
    last_err = ""
    for attempt in range(retries + 1):
        proc = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout), "-A", _UA, url],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            last_err = f"curl exit {proc.returncode} for {url[:80]}"
            if attempt < retries:
                time.sleep(1)
                continue
            _log(f"FETCH FAILED ({last_err}) — returning None; caller may see truncated data")
            return None
        out = proc.stdout
        if not out.strip():
            return None  # legitimate empty body (curl succeeded)
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            _log(f"non-JSON body for {url[:80]} — returning None")
            return None
    return None

def fetch_resolved_markets(max_markets: int = 2000) -> list[dict]:
    """Paginate closed markets (API caps 100/page; offset advances)."""
    out: list[dict] = []
    for off in range(0, max_markets, 100):
        page = _get(f"{GAMMA}/markets?closed=true&limit=100&offset={off}")
        page = page if isinstance(page, list) else (page or {}).get("data", [])
        if not page:
            break
        out.extend(page)
    return out

def fetch_market_by_id(market_id: int | str) -> Optional[dict]:
    """Authoritative single-market lookup (NOT conditionId — that collides)."""
    m = _get(f"{GAMMA}/markets/{market_id}")
    if isinstance(m, list):
        return m[0] if m else None
    return m if isinstance(m, dict) else None

def fetch_trades(condition_id: str, limit: int = 1000) -> list[dict]:
    """Trades for a market. Caller MUST re-validate each trade belongs to the
    intended market (conditionId collides)."""
    t = _get(f"{DATA}/trades?conditionId={condition_id}&limit={limit}")
    return t if isinstance(t, list) else (t or {}).get("data", [])
