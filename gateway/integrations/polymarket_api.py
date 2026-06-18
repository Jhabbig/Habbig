"""Thin Polymarket network layer. The Polymarket REST endpoints (gamma-api,
data-api) are dead from this host, so credibility data now comes from the
Goldsky-hosted Polymarket orderbook subgraph (free, no auth). Pure I/O — no
business logic here (that lives in polymarket_ingest.py, unit-tested).

OPS NOTE: from this datacenter IP the subgraph INTERMITTENTLY returns an empty
body (rate-limit/blip). An empty body is NOT 'no data' — every query MUST
retry-on-empty or you silently get 0 rows. `gql()` enforces this."""
from __future__ import annotations
import json, subprocess, sys, time

SUBGRAPH = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/subgraphs/"
    "polymarket-orderbook-resync/prod/gn"
)
_UA = "Mozilla/5.0"

def _log(msg: str) -> None:
    print(f"[polymarket_api] {msg}", file=sys.stderr)

def gql(query: str, retries: int = 5, timeout: int = 40) -> dict | None:
    """POST a GraphQL query to the subgraph via curl. RETRY-ON-EMPTY: this IP
    flakily returns an empty/non-JSON body even on success, so retry up to
    `retries` times (2s sleep) before giving up. Returns the parsed response
    dict on success, or None after exhausting retries."""
    payload = json.dumps({"query": query})
    for attempt in range(1, retries + 1):
        proc = subprocess.run(
            [
                "curl", "-s", "--max-time", str(timeout),
                "-A", _UA,
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "-d", payload,
                SUBGRAPH,
            ],
            capture_output=True, text=True,
        )
        out = proc.stdout
        if proc.returncode != 0:
            _log(f"curl exit {proc.returncode} (attempt {attempt}/{retries})")
        elif not out.strip():
            _log(f"empty body (attempt {attempt}/{retries}) — retrying (NOT 'no data')")
        else:
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                _log(f"non-JSON body (attempt {attempt}/{retries}) — retrying")
        if attempt < retries:
            time.sleep(2)
    _log(f"gql FAILED after {retries} attempts — returning None")
    return None

def fetch_market_profits(first: int = 1000, skip: int = 0) -> list[dict]:
    """One page of per-wallet per-market realized P&L. Returns the list of
    marketProfit rows (each with nested condition), or [] on failure."""
    query = (
        "{ marketProfits(first: %d, skip: %d) {"
        " id user { id } scaledProfit"
        " condition { id resolutionTimestamp payoutNumerators payoutDenominator }"
        " } }" % (first, skip)
    )
    resp = gql(query)
    if not resp:
        return []
    rows = (resp.get("data") or {}).get("marketProfits")
    return rows if isinstance(rows, list) else []

def fetch_all_market_profits(max_rows: int = 3000) -> list[dict]:
    """Paginate marketProfits in pages of 1000 (skip-based) until a short/empty
    page or `max_rows` is reached. Returns the combined list."""
    out: list[dict] = []
    page_size = 1000
    skip = 0
    while len(out) < max_rows:
        first = min(page_size, max_rows - skip)
        page = fetch_market_profits(first=first, skip=skip)
        out.extend(page)
        _log(f"fetched {len(page)} rows at skip={skip} (total {len(out)})")
        if len(page) < first:
            break  # short/empty page — end of data
        skip += page_size
    return out[:max_rows]
