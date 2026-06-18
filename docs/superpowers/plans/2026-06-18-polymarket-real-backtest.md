# Polymarket Real-Data Backtest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a real, auditable accuracy number — "narve's credibility-weighted prediction beat the Polymarket price X% vs Y%" — from real resolved Polymarket markets + real trader history, by building a Polymarket→dataset ingest that feeds the already-built walk-forward harness.

**Architecture:** A new ingest module pulls resolved Polymarket markets + their trades from the public Gamma + data-api APIs, converts each wallet's trades on each market into a dated "forecast" (direction + implied probability), and writes a dataset file in the existing `SCHEMA.md` shape. The existing `backtest_dataset.py` → `backtest_replay.py` → `backtest_accuracy.py` → `backtest_report.py` pipeline (already built + tested on synthetic data) then runs unchanged on the real dataset. The ingest encodes the 3 integrity traps the spike found.

**Tech Stack:** Python 3.9 stdlib + `subprocess`/`curl` for API calls (Python `urllib` is 403-blocked by Polymarket; `curl -A "Mozilla/5.0"` works). pytest for tests. No new dependencies.

---

## Integrity guards (from the 2026-06-18 spike — every one MUST be encoded)

1. **conditionId collides.** `gamma /markets?conditionId=X` returns unrelated markets. Use `/markets/{id}` (unique) for market detail; treat `data-api /trades?conditionId=` results as candidates and re-validate each trade belongs to the intended market via the market's `clobTokenIds` (the trade's `asset` field) OR exact `conditionId` equality on the trade object.
2. **Ambiguous resolutions exist.** Only score markets whose `outcomePrices` are decisive: `{round(p0), round(p1)} == {0,1}` AND `abs(p0-p1) > 0.9`. Reject everything else (record the count rejected).
3. **BUY/SELL × outcome semantics.** A SELL of "Yes" is a bet on "No". The wallet→prediction rule (Task 3) defines this exactly and is unit-tested on hand-verified cases.

## Wallet → prediction rule (the one real modeling decision — explicit + testable)

For each `(wallet, market)`, aggregate that wallet's trades into a net YES position:
- Each trade has `outcome` (the label bought/sold, e.g. "Yes"/"No"), `side` ("BUY"/"SELL"), `size`, `price` (0–1).
- Convert to **signed YES-exposure** per trade:
  - BUY "Yes"  → `+size`  at YES-price `price`
  - SELL "Yes" → `-size`  at YES-price `price`
  - BUY "No"   → `-size`  at YES-price `(1 - price)`
  - SELL "No"  → `+size`  at YES-price `(1 - price)`
- `net = sum(signed size)`. If `net == 0` → wallet made **no net prediction**, skip.
- `direction` = "YES" if `net > 0` else "NO".
- `predicted_probability` (of YES) = size-weighted average YES-price of the trades on the **net side**, clamped to `[0.01, 0.99]`. (Revealed-belief proxy: a wallet that bought YES at avg 0.62 is recorded as predicting P(YES)=0.62. Directional credibility needs the side; this gives Brier a real number.)
- `made_at` = the **earliest** trade timestamp on the net side (most conservative, point-in-time honest).

This is a documented v1 rule; refinement (e.g. recency-weighting, separating conviction from price) is a later iteration, not this plan.

---

## File structure

- **Create** `gateway/integrations/polymarket_ingest.py` — pure functions: resolution-decisiveness check, wallet→prediction rule, market-record builder. No network (testable).
- **Create** `gateway/integrations/polymarket_api.py` — thin network layer: `fetch_resolved_markets()`, `fetch_market_by_id()`, `fetch_trades()`, all via `curl`. Isolated so the pure logic stays unit-testable.
- **Create** `gateway/scripts/build_polymarket_dataset.py` — CLI orchestrator: pull → convert → write `data/backtest/polymarket_real.json` (SCHEMA shape).
- **Create** `gateway/scripts/audit_dataset.py` — prints N markets, N rejected (+why), a sample of 5 markets with their winner + 3 traders' predictions for HAND verification.
- **Create** `gateway/tests/test_polymarket_ingest.py` — unit tests for the pure functions (the integrity guards + the wallet rule).
- **Reuse unchanged:** `backtest_dataset.py`, `backtest_replay.py`, `backtest_accuracy.py`, `backtest_report.py`, `tests/test_backtest_replay.py`.

---

### Task 1: Network layer (isolated, so logic stays testable)

**Files:**
- Create: `gateway/integrations/__init__.py` (empty if missing)
- Create: `gateway/integrations/polymarket_api.py`

- [ ] **Step 1: Write the module**

```python
"""Thin Polymarket network layer. Python urllib is 403-blocked by Polymarket;
curl with a browser UA works, so every call shells out to curl. Pure I/O — no
business logic here (that lives in polymarket_ingest.py, unit-tested)."""
from __future__ import annotations
import json, subprocess
from typing import Any, Optional

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
_UA = "Mozilla/5.0"

def _get(url: str, timeout: int = 20) -> Any:
    out = subprocess.run(["curl", "-s", "--max-time", str(timeout), "-A", _UA, url],
                         capture_output=True, text=True).stdout
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
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
```

- [ ] **Step 2: Smoke-check it imports + reaches the API**

Run: `cd ~/Habbig/gateway && python3 -c "from integrations import polymarket_api as a; ms=a.fetch_resolved_markets(200); print('markets:', len(ms))"`
Expected: `markets:` followed by a number > 100 (real data; if 0, the network is blocked — note it, the pure-logic tasks still proceed).

- [ ] **Step 3: Commit**

```bash
git add gateway/integrations/__init__.py gateway/integrations/polymarket_api.py
git commit -m "feat(backtest): Polymarket network layer (curl-based, urllib is 403-blocked)"
```

---

### Task 2: Resolution-decisiveness guard (integrity trap #2)

**Files:**
- Create: `gateway/integrations/polymarket_ingest.py`
- Test: `gateway/tests/test_polymarket_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
from integrations.polymarket_ingest import decisive_outcome

def test_decisive_outcome_clean_yes():
    # outcomePrices ~[1,0], outcomes ["Yes","No"] -> YES won (index 0)
    assert decisive_outcome(["0.999999", "0.000001"], ["Yes", "No"]) == (1, "Yes")

def test_decisive_outcome_clean_no():
    assert decisive_outcome(["0.0000001", "0.9999"], ["Yes", "No"]) == (0, "No")

def test_decisive_outcome_rejects_ambiguous():
    assert decisive_outcome(["0.52", "0.48"], ["Yes", "No"]) is None
    assert decisive_outcome(["0", "0"], ["Yes", "No"]) is None
    assert decisive_outcome(None, ["Yes", "No"]) is None
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd ~/Habbig/gateway && python3 -m pytest tests/test_polymarket_ingest.py -q`
Expected: FAIL (ImportError / module not found).

- [ ] **Step 3: Implement**

```python
"""Pure Polymarket -> dataset conversion. No network (see polymarket_api.py).
Encodes the 3 integrity guards from the 2026-06-18 spike + the wallet->prediction
rule. Output records match data/backtest/SCHEMA.md exactly."""
from __future__ import annotations
import json
from typing import Optional

def _as_list(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return None
    return v

def decisive_outcome(outcome_prices, outcomes) -> Optional[tuple[int, str]]:
    """Return (resolved_outcome 1/0 for YES, winning_label) ONLY if the market
    resolved decisively. Else None. resolved_outcome is 1 iff the YES outcome
    (index 0) won. Integrity guard #2 — reject ambiguous resolutions."""
    op = _as_list(outcome_prices); oc = _as_list(outcomes)
    if not op or not oc or len(op) != 2 or len(oc) != 2:
        return None
    try:
        a, b = float(op[0]), float(op[1])
    except (TypeError, ValueError):
        return None
    if {round(a), round(b)} != {0, 1} or abs(a - b) <= 0.9:
        return None
    win_idx = 0 if a > b else 1
    resolved_outcome = 1 if win_idx == 0 else 0   # index 0 == "Yes" by Polymarket convention
    return resolved_outcome, oc[win_idx]
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd ~/Habbig/gateway && python3 -m pytest tests/test_polymarket_ingest.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add gateway/integrations/polymarket_ingest.py gateway/tests/test_polymarket_ingest.py
git commit -m "feat(backtest): decisive-resolution guard (rejects ambiguous markets)"
```

---

### Task 3: Wallet → prediction rule (the modeling decision + integrity trap #3)

**Files:**
- Modify: `gateway/integrations/polymarket_ingest.py`
- Test: `gateway/tests/test_polymarket_ingest.py`

- [ ] **Step 1: Write the failing tests (hand-verified cases)**

```python
from integrations.polymarket_ingest import wallet_prediction

def _trade(outcome, side, size, price, ts):
    return {"outcome": outcome, "side": side, "size": size, "price": price, "timestamp": ts}

def test_buy_yes_predicts_yes():
    p = wallet_prediction([_trade("Yes", "BUY", 100, 0.60, 1000)])
    assert p["direction"] == "YES"
    assert abs(p["predicted_probability"] - 0.60) < 1e-6
    assert p["made_at_ts"] == 1000

def test_sell_yes_is_a_bet_on_no():
    # SELL Yes @0.60 == bet on NO (integrity trap #3)
    p = wallet_prediction([_trade("Yes", "SELL", 100, 0.60, 2000)])
    assert p["direction"] == "NO"

def test_buy_no_predicts_no_with_yes_implied_price():
    # BUY No @0.30 -> YES-implied price 0.70, net YES exposure negative -> NO
    p = wallet_prediction([_trade("No", "BUY", 100, 0.30, 1500)])
    assert p["direction"] == "NO"

def test_net_zero_is_no_prediction():
    assert wallet_prediction([
        _trade("Yes", "BUY", 100, 0.50, 1),
        _trade("Yes", "SELL", 100, 0.50, 2),
    ]) is None

def test_earliest_timestamp_on_net_side():
    p = wallet_prediction([
        _trade("Yes", "BUY", 100, 0.55, 3000),
        _trade("Yes", "BUY", 100, 0.65, 1000),
    ])
    assert p["made_at_ts"] == 1000
    assert abs(p["predicted_probability"] - 0.60) < 1e-6  # size-weighted avg
```

- [ ] **Step 2: Run, verify fail**

Run: `cd ~/Habbig/gateway && python3 -m pytest tests/test_polymarket_ingest.py -k wallet -q`
Expected: FAIL (wallet_prediction not defined).

- [ ] **Step 3: Implement**

```python
def _yes_signed(trade) -> Optional[tuple[float, float, int]]:
    """Return (signed_yes_size, yes_price, ts) for one trade, or None if unusable.
    Integrity trap #3: SELL of an outcome bets against it."""
    try:
        size = float(trade.get("size")); price = float(trade.get("price"))
        ts = int(trade.get("timestamp"))
    except (TypeError, ValueError):
        return None
    label = str(trade.get("outcome", "")).strip().lower()
    side = str(trade.get("side", "")).strip().upper()
    if side not in ("BUY", "SELL") or label not in ("yes", "no"):
        return None
    yes_price = price if label == "yes" else (1.0 - price)
    # YES-exposure sign: BUY Yes / SELL No = +ve ; SELL Yes / BUY No = -ve
    if label == "yes":
        signed = size if side == "BUY" else -size
    else:
        signed = size if side == "SELL" else -size
    return signed, yes_price, ts

def wallet_prediction(trades: list[dict]) -> Optional[dict]:
    """Aggregate a wallet's trades on ONE market into a single dated prediction.
    See the 'Wallet -> prediction rule' section of the plan. Returns None when
    the wallet has no net position. predicted_probability is P(YES)."""
    legs = [x for x in (_yes_signed(t) for t in trades) if x is not None]
    if not legs:
        return None
    net = sum(s for s, _, _ in legs)
    if abs(net) < 1e-9:
        return None
    direction = "YES" if net > 0 else "NO"
    # size-weighted avg YES-price over legs on the NET side
    net_side_pos = net > 0
    side_legs = [(abs(s), yp, ts) for s, yp, ts in legs if (s > 0) == net_side_pos]
    wsum = sum(w for w, _, _ in side_legs) or 1.0
    yes_vwap = sum(w * yp for w, yp, _ in side_legs) / wsum
    pred_yes = yes_vwap if direction == "YES" else yes_vwap  # vwap already YES-priced
    pred_yes = max(0.01, min(0.99, pred_yes))
    made_at_ts = min(ts for _, _, ts in side_legs)
    return {"direction": direction, "predicted_probability": round(pred_yes, 6),
            "made_at_ts": made_at_ts}
```

- [ ] **Step 4: Run, verify pass**

Run: `cd ~/Habbig/gateway && python3 -m pytest tests/test_polymarket_ingest.py -k wallet -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add gateway/integrations/polymarket_ingest.py gateway/tests/test_polymarket_ingest.py
git commit -m "feat(backtest): wallet->prediction rule (BUY/SELL semantics, net position, VWAP)"
```

---

### Task 4: Market-record builder (assembles a SCHEMA.md record)

**Files:**
- Modify: `gateway/integrations/polymarket_ingest.py`
- Test: `gateway/tests/test_polymarket_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
from integrations.polymarket_ingest import build_market_record
import datetime as dt

def test_build_market_record_shape():
    market = {
        "id": 19, "slug": "kanye-divorce", "question": "Will they divorce?",
        "conditionId": "0xabc", "endDate": "2024-01-01T00:00:00Z",
        "outcomes": '["Yes","No"]', "outcomePrices": '["0.0000001","0.9999"]',
    }
    trades = [
        {"proxyWallet": "0xWALLET1", "outcome": "No", "side": "BUY", "size": 100, "price": 0.40, "timestamp": 1700000000},
        {"proxyWallet": "0xWALLET2", "outcome": "Yes", "side": "BUY", "size": 50, "price": 0.55, "timestamp": 1700000100},
    ]
    rec = build_market_record(market, trades)
    assert rec["market_id"] == "kanye-divorce"
    assert rec["resolved_outcome"] == 0          # "No" won
    assert rec["resolved_at"].startswith("2024-01-01")
    assert len(rec["price_timeline"]) >= 1
    handles = {f["source_handle"] for f in rec["forecasts"]}
    assert handles == {"0xWALLET1", "0xWALLET2"}
    # each forecast made_at is BEFORE resolved_at (lookahead-safe for the harness)
    for f in rec["forecasts"]:
        assert f["made_at"] < rec["resolved_at"]

def test_build_market_record_none_when_ambiguous():
    market = {"id": 1, "slug": "x", "question": "q", "conditionId": "0x",
              "endDate": "2024-01-01T00:00:00Z",
              "outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]'}
    assert build_market_record(market, []) is None
```

- [ ] **Step 2: Run, verify fail**

Run: `cd ~/Habbig/gateway && python3 -m pytest tests/test_polymarket_ingest.py -k build_market -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
import datetime as _dt

def _ts_to_iso(ts: int) -> str:
    return _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def build_market_record(market: dict, trades: list[dict]) -> Optional[dict]:
    """Build one SCHEMA.md market record from a resolved market + its trades.
    Returns None if the market isn't decisively resolved (guard #2) or has no
    usable forecasts. Forecasts whose made_at is not strictly before resolution
    are dropped (lookahead-safe)."""
    dec = decisive_outcome(market.get("outcomePrices"), market.get("outcomes"))
    if dec is None:
        return None
    resolved_outcome, _win = dec
    resolved_at = str(market.get("endDate") or market.get("updatedAt") or "")
    if not resolved_at:
        return None
    # group trades by wallet
    by_wallet: dict[str, list[dict]] = {}
    for t in trades:
        w = t.get("proxyWallet")
        if w:
            by_wallet.setdefault(w, []).append(t)
    forecasts = []
    for wallet, wtr in by_wallet.items():
        pred = wallet_prediction(wtr)
        if pred is None:
            continue
        made_at = _ts_to_iso(pred["made_at_ts"])
        if made_at >= resolved_at:      # lookahead-safe: drop predictions at/after resolution
            continue
        forecasts.append({
            "source_handle": wallet,
            "predicted_probability": pred["predicted_probability"],
            "made_at": made_at,
            "url": f"https://polymarket.com/market/{market.get('slug','')}",
        })
    if not forecasts:
        return None
    # price timeline: use lastTradePrice as a single point if no history endpoint;
    # the harness needs at least one YES-price point per market.
    try:
        last = float(market.get("lastTradePrice"))
    except (TypeError, ValueError):
        op = _as_list(market.get("outcomePrices")) or [0.5, 0.5]
        last = float(op[0])
    price_timeline = [{"date": resolved_at[:10], "yes_price": max(0.0, min(1.0, last))}]
    return {
        "market_id": str(market.get("slug") or market.get("id")),
        "question": str(market.get("question", "")),
        "resolved_outcome": resolved_outcome,
        "resolved_at": resolved_at,
        "price_timeline": price_timeline,
        "forecasts": forecasts,
    }
```

- [ ] **Step 4: Run, verify pass**

Run: `cd ~/Habbig/gateway && python3 -m pytest tests/test_polymarket_ingest.py -k build_market -q`
Expected: 2 passed.

> **NOTE (price_timeline limitation):** v1 uses a single `lastTradePrice` point. The
> CLOB `/prices-history` endpoint gives a real intraday timeline; wiring it in is a
> Task-8 follow-up. The harness only requires ≥1 point, so v1 runs; the market-price
> baseline is coarser until then. Flag this in the report, do NOT hide it.

- [ ] **Step 5: Commit**

```bash
git add gateway/integrations/polymarket_ingest.py gateway/tests/test_polymarket_ingest.py
git commit -m "feat(backtest): build SCHEMA market records from real Polymarket markets+trades"
```

---

### Task 5: Dataset builder CLI (pull real data → write the dataset)

**Files:**
- Create: `gateway/scripts/build_polymarket_dataset.py`

- [ ] **Step 1: Write the CLI**

```python
"""Pull real resolved Polymarket markets + trades, convert via polymarket_ingest,
write data/backtest/polymarket_real.json (SCHEMA.md shape). Run from gateway/:
    python3 scripts/build_polymarket_dataset.py --max-markets 800 --min-trades 30
"""
from __future__ import annotations
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from integrations import polymarket_api as api
from integrations import polymarket_ingest as ing

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=800)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--out", default="data/backtest/polymarket_real.json")
    a = ap.parse_args()

    raw = api.fetch_resolved_markets(a.max_markets)
    print(f"pulled {len(raw)} closed markets")
    records, rejected = [], {"ambiguous": 0, "no_trades": 0, "no_forecasts": 0}
    for m in raw:
        if ing.decisive_outcome(m.get("outcomePrices"), m.get("outcomes")) is None:
            rejected["ambiguous"] += 1
            continue
        cond = m.get("conditionId")
        if not cond:
            continue
        trades = api.fetch_trades(cond, limit=1000)
        # integrity guard #1: keep only trades whose conditionId matches this market
        trades = [t for t in trades if str(t.get("conditionId")) == str(cond)]
        if len(trades) < a.min_trades:
            rejected["no_trades"] += 1
            continue
        rec = ing.build_market_record(m, trades)
        if rec is None:
            rejected["no_forecasts"] += 1
            continue
        records.append(rec)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump({"source": "polymarket", "synthetic": False, "markets": records},
              open(a.out, "w"), indent=2)
    print(f"wrote {len(records)} markets -> {a.out}")
    print(f"rejected: {rejected}")
    print(f"total forecasts: {sum(len(r['forecasts']) for r in records)}")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it on real data**

Run: `cd ~/Habbig/gateway && python3 scripts/build_polymarket_dataset.py --max-markets 800 --min-trades 30`
Expected: prints `wrote N markets` (N ≥ 20 hoped), a `rejected:` breakdown, and a total-forecasts count in the hundreds+. If network-blocked, note it and run from the prod box.

- [ ] **Step 3: Validate the output loads in the existing loader**

Run: `cd ~/Habbig/gateway && python3 -c "import backtest_dataset as d; ds=d.load_dataset('data/backtest/polymarket_real.json'); print('loaded', len(ds), 'real markets')"`
Expected: `loaded N real markets` with no validation error.

- [ ] **Step 4: Commit (code + dataset)**

```bash
git add gateway/scripts/build_polymarket_dataset.py gateway/data/backtest/polymarket_real.json
git commit -m "feat(backtest): CLI builds real Polymarket dataset (SCHEMA shape) + first pull"
```

---

### Task 6: Audit script (hand-verifiability — the anti-Theranos gate)

**Files:**
- Create: `gateway/scripts/audit_dataset.py`

- [ ] **Step 1: Write it**

```python
"""Print a human-auditable summary of a dataset so a person can hand-verify the
trade->outcome join is honest. Run: python3 scripts/audit_dataset.py data/backtest/polymarket_real.json"""
from __future__ import annotations
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtest_dataset as d

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/backtest/polymarket_real.json"
    ds = d.load_dataset(path)
    print(f"DATASET: {path}")
    print(f"  markets: {len(ds)}")
    print(f"  total forecasts: {sum(len(m['forecasts']) for m in ds)}")
    print(f"  distinct forecasters: {len({f['source_handle'] for m in ds for f in m['forecasts']})}")
    print("\n  SAMPLE (hand-verify winner vs forecasters):")
    for m in ds[:5]:
        won = "YES" if m["resolved_outcome"] == 1 else "NO"
        print(f"\n  - {m['question'][:64]}")
        print(f"    resolved: {won}  ({m['resolved_at'][:10]})  market_id={m['market_id']}")
        for f in m["forecasts"][:3]:
            side = "YES" if f["predicted_probability"] >= 0.5 else "NO"
            mark = "✓" if side == won else "✗"
            print(f"      {f['source_handle'][:12]} predicted {side} (p={f['predicted_probability']}) {mark}")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run + eyeball**

Run: `cd ~/Habbig/gateway && python3 scripts/audit_dataset.py data/backtest/polymarket_real.json`
Expected: 5 real markets with winner + 3 forecasters each, ✓/✗ marks. **Manually sanity-check 2–3 against polymarket.com** — this is the gate that catches a fake join.

- [ ] **Step 3: Commit**

```bash
git add gateway/scripts/audit_dataset.py
git commit -m "feat(backtest): dataset audit script for hand-verifying join honesty"
```

---

### Task 7: Run the real backtest + accuracy report

**Files:**
- Modify (if needed): none — reuse `backtest_accuracy.py` + `backtest_report.py`.

- [ ] **Step 1: Run the accuracy scorer on the real dataset, both methods**

Run: `cd ~/Habbig/gateway && python3 backtest_accuracy.py data/backtest/polymarket_real.json`
Expected: prints narve vs market accuracy + Brier for `strict_two_window` and `bayesian`. **This is the real number.** Record it.

- [ ] **Step 2: Generate the report**

Run:
```bash
cd ~/Habbig/gateway && python3 -c "
import backtest_dataset as d, backtest_replay as r, backtest_report as rep
ds = d.load_dataset('data/backtest/polymarket_real.json')
outs = {m: r.run_replay(ds, cold_start=m) for m in ('strict_two_window','bayesian')}
rep.build_report(outs)
print('report written')
"`
```
Expected: `data/backtest/report.md` + `report.html` regenerated from REAL data.

- [ ] **Step 3: Honesty check the result**

Read `data/backtest/report.md`. Confirm: N is stated, narve-vs-market accuracy is shown for both methods, per-bet/per-market detail is present. If narve does NOT beat the market, **report that honestly** — a real negative result is information, not a failure to hide. Note the price_timeline-is-single-point caveat (Task 4).

- [ ] **Step 4: Commit the report**

```bash
git add gateway/data/backtest/report.md gateway/data/backtest/report.html
git commit -m "feat(backtest): first REAL accuracy report on Polymarket data"
```

---

### Task 8 (follow-up, optional): real intraday price timeline

**Files:** Modify `gateway/integrations/polymarket_api.py` + `polymarket_ingest.py`

- [ ] Add `fetch_price_history(clob_token_id)` hitting `https://clob.polymarket.com/prices-history?market=<token>&interval=max`, map to `[{date, yes_price}]`, and use it in `build_market_record` instead of the single `lastTradePrice` point. Re-run Tasks 5–7. (Improves the market baseline; not required for a first real number.)

---

## Appendix — the 20–30 actionable items (meeting ask, split by who/when)

**A. Demo critical path (this plan, in order):** 1) network layer, 2) resolution guard, 3) wallet rule, 4) record builder, 5) dataset CLI + first pull, 6) audit + hand-verify, 7) real accuracy report, 8) hand-check 3 markets on polymarket.com, 9) intraday price timeline (Task 8), 10) widen to ~2,000 markets, 11) filter to a politics subset + compare, 12) wire the proven number onto `/markets/active` (Phase 2 of the spec), 13) write the one-paragraph "how we measured it" for the deck.

**B. Founder / manual (parallel, blocked on you or the box):** 14) get the prod box reachable (Tailscale) — unblocks deploy + Metaculus, 15) deploy the committed gate-bypass security fix, 16) install Playwright on prod, 17) Twitter/TruthSocial session logins, 18) start the scraper, 19) confirm live predictions flow into the DB, 20) set `SCRAPER_API_KEY` (already wired) end-to-end, 21) provide any specific forecasters you want featured (optional now that traders are the source), 22) run the dataset build from the prod box to unlock Metaculus as source #2.

**C. Pitch / business (founder-owned):** 23) pitch deck with the real accuracy number + the audit trail as the "it works" slide, 24) one-line thesis + baseline framing, 25) Slack workspace for coordination, 26) SF investor meeting prep, 27) YC application draft referencing the backtest.

## Out of scope (per spec)
UI polish, Stripe, multi-vertical, X/Reddit/Truth scraping for v1, betting-ROI as a headline.
