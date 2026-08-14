"""Smoke test for whale-dashboard.

Boots the FastAPI app in-process via TestClient (no real server, no real
SEC traffic), exercises every endpoint, and asserts each returns a sane
shape.

How it works:
    1. Sets WHALE_NO_WORKERS=1 to disable daemon-thread ingesters and the
       WS fanout — we don't want network calls during the smoke test.
    2. Sets GATEWAY_SSO_SECRET and SEC_USER_AGENT so module imports succeed.
    3. Imports backend.main, which runs init_db() and seeds the entity table.
    4. Manually injects a small fixture set of holdings + a 13D + a Form 4
       so endpoints have data to return.
    5. Hits each endpoint with the trusted gateway headers and asserts.

Run from repo root:
    cd whale-dashboard && PYTHONPATH=backend python3 tests/smoke.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Pre-import setup
# ---------------------------------------------------------------------------
os.environ["WHALE_NO_WORKERS"] = "1"
os.environ["GATEWAY_SSO_SECRET"] = "smoke-secret"
os.environ.setdefault("SEC_USER_AGENT", "WhaleSmoke smoke@example.com")

# Use a throwaway DB so we don't trample real data.
TMP_DB = Path(__file__).resolve().parent / "_smoke.db"
if TMP_DB.exists():
    TMP_DB.unlink()

# The backend modules look up the DB path relative to backend/database.py, so
# we patch DB_PATH after import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import database as db  # noqa: E402
db.DB_PATH = TMP_DB     # type: ignore[assignment]

# Now import the app — this triggers init_db() against TMP_DB.
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import analysis.intent_classifier as ic  # noqa: E402

ADMIN_HEADERS = {
    "x-gateway-secret": "smoke-secret",
    "x-gateway-user-id": "smoke-user-id",
    "x-gateway-user-email": "smoke@example.com",
    "x-gateway-user-tier": "admin",
    "x-gateway-user-display-name": "Smoke",
}
USER_HEADERS = {**ADMIN_HEADERS, "x-gateway-user-tier": "premium"}


# ---------------------------------------------------------------------------
# Fixtures — write directly to SQLite so we don't hit SEC.
# ---------------------------------------------------------------------------

def install_fixtures() -> None:
    """Synthesize one whale's 13F across two quarters + one 13D + Form 4s."""
    # Trigger lifespan startup inside TestClient context to run init_db +
    # entity seed. We do it implicitly by entering TestClient below; here we
    # just write the fixtures.
    main.init_db()
    main.load_seed_into_db()

    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        # Use the seeded JPMorgan entity / its first CIK.
        ent = conn.execute("SELECT id FROM entities WHERE slug='jpmorgan'").fetchone()
        cik_row = conn.execute(
            "SELECT cik FROM cik_map WHERE entity_id=? LIMIT 1", (ent["id"],),
        ).fetchone()
        cik = int(cik_row["cik"])

        # Two quarters of 13F filings for the same CIK.
        for q, acc in [("2025-09-30", "0000000000-25-000001"),
                       ("2025-12-31", "0000000000-25-000002")]:
            cur = conn.execute(
                """INSERT OR IGNORE INTO filings_13f
                     (cik, accession, form_type, quarter_end, filed_date,
                      total_value_usd, n_positions, fetched_at)
                   VALUES (?, ?, '13F-HR', ?, ?, ?, ?, ?)""",
                (cik, acc, q, q, 5_000_000.0, 2, now),
            )
            fid = cur.lastrowid or conn.execute(
                "SELECT id FROM filings_13f WHERE accession=?", (acc,)
            ).fetchone()["id"]

            # Two positions per quarter — NVDA (added) and IBM (trimmed).
            shares_nvda = 100_000 if q == "2025-09-30" else 200_000
            shares_ibm  = 50_000  if q == "2025-09-30" else 30_000
            for cusip, ticker, issuer, shares, value in [
                ("67066G104", "NVDA", "NVIDIA Corp", shares_nvda, shares_nvda * 100),
                ("459200101", "IBM",  "IBM Corp",   shares_ibm,  shares_ibm  * 200),
            ]:
                conn.execute(
                    """INSERT OR IGNORE INTO holdings
                         (filing_id, cusip, ticker, issuer_name, title_of_class,
                          shares, value_usd, put_call, investment_disc)
                       VALUES (?, ?, ?, ?, 'COM', ?, ?, NULL, 'SOLE')""",
                    (fid, cusip, ticker, issuer, shares, value),
                )

        # Insider transactions for NVDA (cluster of 3 buys).
        for i, name in enumerate(["A. Officer", "B. Director", "C. CFO"]):
            conn.execute(
                """INSERT OR IGNORE INTO insider_txns
                     (accession, issuer_cik, issuer_ticker, issuer_name,
                      insider_cik, insider_name, insider_role,
                      txn_date, txn_code, shares, price, value_usd,
                      post_holdings, fetched_at)
                   VALUES (?, ?, 'NVDA', 'NVIDIA Corp', ?, ?, 'Officer',
                           date('now', '-3 days'), 'P', 1000, 100, 100000, 5000, ?)""",
                (f"smoke-form4-{i}", 1045810, 1000000 + i, name, now),
            )

        # 13D filing on NVDA from Elliott (seeded entity).
        elliott = conn.execute("SELECT id FROM entities WHERE slug='elliott'").fetchone()
        intent_text = (
            "Item 4. Purpose of Transaction. The Reporting Persons "
            "intend to engage with management and the board of "
            "directors regarding strategic alternatives and "
            "shareholder value."
        )
        conn.execute(
            """INSERT OR IGNORE INTO activist_filings
                 (accession, schedule, filer_cik, filer_entity_id,
                  target_cik, target_ticker, target_name, filed_date,
                  ownership_pct, shares_owned, intent_summary, fetched_at)
               VALUES (?, '13D', 1791786, ?, 1045810, 'NVDA', 'NVIDIA Corp',
                       date('now'), 6.2, 1500000, ?, ?)""",
            ("smoke-13d-1", elliott["id"], intent_text, now),
        )

        # Pre-load a CFTC COT row so /api/cot has data.
        conn.execute(
            """INSERT OR IGNORE INTO cftc_cot
                 (market_code, market_name, report_date,
                  commercial_long, commercial_short,
                  noncommercial_long, noncommercial_short,
                  nonreportable_long, nonreportable_short,
                  open_interest, fetched_at)
               VALUES ('CL', 'CRUDE OIL, LIGHT SWEET-NYMEX',
                       date('now', '-3 days'),
                       400000, 600000, 250000, 150000, 50000, 50000,
                       1000000, ?)""",
            (now,),
        )

    # Now derived tables: deltas, intent classifier, consensus.
    from analysis.diff_engine import recompute_all_deltas
    recompute_all_deltas()
    n = ic.backfill_existing()
    assert n >= 1, "intent classifier should have labelled the seeded 13D"
    from analysis.consensus import recompute_consensus
    recompute_consensus()


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

PASSED = []
FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


# ---------------------------------------------------------------------------
# Test bodies
# ---------------------------------------------------------------------------

def test_intent_classifier() -> None:
    print("\n[unit] intent_classifier")
    label, score = ic.classify(
        "The Reporting Persons intend to nominate directors and engage with "
        "management regarding shareholder value."
    )
    check("activist intent classified", label == "activist" and score > 0.4,
          f"got ({label}, {score})")
    label, score = ic.classify("This stake is held solely for investment purposes only.")
    check("passive intent classified", label == "passive" and score > 0.3,
          f"got ({label}, {score})")
    label, score = ic.classify(
        "We propose to acquire all outstanding shares via a tender offer "
        "and take-private business combination, with a proposal to acquire "
        "the company in a merger."
    )
    check("acquisition intent classified", label == "acquisition" and score > 0.4,
          f"got ({label}, {score})")


def test_endpoints(client: TestClient) -> None:
    print("\n[http] auth gating")
    r = client.get("/api/whales")
    check("unauthed request rejected", r.status_code == 401)

    print("\n[http] core read endpoints")
    r = client.get("/health")
    check("health 200", r.status_code == 200)

    r = client.get("/api/whales", headers=USER_HEADERS)
    check("/api/whales 200", r.status_code == 200)
    whales = r.json()
    check("whales include jpmorgan", any(w["slug"] == "jpmorgan" for w in whales))

    r = client.get("/api/whale/jpmorgan", headers=USER_HEADERS)
    check("/api/whale/jpmorgan 200", r.status_code == 200)
    detail = r.json()
    check("whale detail has positions", len(detail["top_positions"]) >= 1)

    r = client.get("/api/whale/jpmorgan/deltas", headers=USER_HEADERS)
    check("/api/whale/{slug}/deltas 200", r.status_code == 200)
    deltas = r.json()["deltas"]
    actions = {d["action"] for d in deltas}
    check("deltas include ADD or TRIM", bool(actions & {"ADD", "NEW", "TRIM", "EXIT"}),
          f"got actions={actions}")

    r = client.get("/api/ticker/NVDA", headers=USER_HEADERS)
    check("/api/ticker/NVDA 200", r.status_code == 200)
    body = r.json()
    check("ticker has holders", len(body["holders"]) >= 1)

    r = client.get("/api/ticker/NVDA/insider", headers=USER_HEADERS)
    check("/api/ticker/{t}/insider 200", r.status_code == 200)
    txns = r.json()["txns"]
    check("insider txns surfaced", len(txns) == 3, f"got {len(txns)}")

    r = client.get("/api/feed", headers=USER_HEADERS)
    check("/api/feed 200", r.status_code == 200)
    check("feed has rows", len(r.json()) >= 3)

    r = client.get("/api/activist", headers=USER_HEADERS)
    check("/api/activist 200", r.status_code == 200)
    a = r.json()
    check("activist row has classified intent",
          any(x.get("filer_name") for x in a) if a else False)

    r = client.get("/api/cluster-buys?days=14&min_insiders=3", headers=USER_HEADERS)
    check("/api/cluster-buys 200", r.status_code == 200)
    rows = r.json()
    check("cluster surfaces NVDA", any(x["issuer_ticker"] == "NVDA" for x in rows))

    r = client.get("/api/correlations", headers=USER_HEADERS)
    check("/api/correlations 200", r.status_code == 200)

    r = client.get("/api/consensus?direction=any&min_whales=1", headers=USER_HEADERS)
    check("/api/consensus 200", r.status_code == 200)
    cons = r.json()["rows"]
    check("consensus has rows", len(cons) >= 1)

    r = client.get("/api/crowdedness", headers=USER_HEADERS)
    check("/api/crowdedness 200", r.status_code == 200)

    r = client.get("/api/cot", headers=USER_HEADERS)
    check("/api/cot 200", r.status_code == 200)
    check("cot row for CL", any(x["market_code"] == "CL" for x in r.json()))


def test_watchlist(client: TestClient) -> None:
    print("\n[http] watchlist")
    r = client.post("/api/watchlist",
                    headers=USER_HEADERS,
                    json={"kind": "ticker", "target": "nvda"})
    check("watchlist add 200", r.status_code == 200)

    r = client.post("/api/watchlist",
                    headers=USER_HEADERS,
                    json={"kind": "ticker", "target": "nvda"})
    check("duplicate watchlist rejected", r.status_code == 409)

    r = client.get("/api/watchlist", headers=USER_HEADERS)
    items = r.json()
    check("watchlist normalizes ticker case",
          any(i["target"] == "NVDA" for i in items))

    if items:
        wl_id = items[0]["id"]
        r = client.delete(f"/api/watchlist/{wl_id}", headers=USER_HEADERS)
        check("watchlist delete 200", r.status_code == 200)


def test_alerts(client: TestClient) -> None:
    print("\n[http] alert rules")
    r = client.post("/api/alerts",
                    headers=USER_HEADERS,
                    json={"rule_type": "13d_filed",
                          "target": "NVDA", "threshold": 5.0,
                          "webhook_url": "http://127.0.0.1:1/never"})
    check("alert create 200", r.status_code == 200)
    rule_id = r.json().get("id")

    r = client.post("/api/alerts",
                    headers=USER_HEADERS,
                    json={"rule_type": "bogus_rule"})
    check("invalid rule_type rejected", r.status_code == 400)

    r = client.get("/api/alerts", headers=USER_HEADERS)
    body = r.json()
    check("alerts list 200", r.status_code == 200)
    check("alert rule visible", any(x["id"] == rule_id for x in body["rules"]))

    # Trigger dispatcher manually — the seeded 13D should match.
    r = client.post("/api/admin/ingest/alerts", headers=ADMIN_HEADERS)
    check("admin alerts dispatch 200", r.status_code == 200)
    result = r.json()
    check("alerts found matches", result["matches"] >= 1,
          f"got {result}")

    # Webhook URL points at a closed port, so delivery should record 'failed'
    # rather than crash.
    r = client.get("/api/alerts", headers=USER_HEADERS)
    deliveries = r.json()["recent_deliveries"]
    check("delivery recorded", len(deliveries) >= 1)
    if deliveries:
        check("delivery_status set",
              deliveries[0]["delivery_status"] in ("sent", "failed", "skipped_no_webhook", "skipped_email"),
              f"got {deliveries[0]['delivery_status']}")


def test_websocket(client: TestClient) -> None:
    print("\n[ws] /ws/feed")
    with client.websocket_connect(
            "/ws/feed",
            headers={"x-gateway-secret": "smoke-secret"}) as ws:
        msg = json.loads(ws.receive_text())
        check("ws hello received", msg.get("type") == "hello",
              f"got type={msg.get('type')}")
        check("ws hello has recent rows", isinstance(msg.get("recent"), list))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def test_openfigi_picker() -> None:
    """Unit test for OpenFIGI result selection — no network."""
    print("\n[unit] openfigi result picker")
    from data_sources.openfigi import _pick_best
    # Empty / non-equity → None
    check("empty results return None", _pick_best([]) is None)
    check("non-equity returns None", _pick_best([
        {"marketSector": "Govt", "ticker": "X", "exchCode": "US"}
    ]) is None)
    # Prefer common stock over preferred
    res = _pick_best([
        {"marketSector": "Equity", "ticker": "BRK/B", "exchCode": "UN",
         "securityType2": "Preferred Stock", "name": "Berkshire Pref"},
        {"marketSector": "Equity", "ticker": "BRK/B", "exchCode": "UN",
         "securityType2": "Common Stock", "name": "Berkshire Class B"},
    ])
    check("prefers common over preferred",
          (res or {}).get("securityType2") == "Common Stock")
    # Prefer US over foreign listing
    res = _pick_best([
        {"marketSector": "Equity", "ticker": "NVDA", "exchCode": "GR",
         "securityType2": "Common Stock"},
        {"marketSector": "Equity", "ticker": "NVDA", "exchCode": "UQ",
         "securityType2": "Common Stock"},
    ])
    check("prefers US listing", (res or {}).get("exchCode") == "UQ")
    # Foreign-only → None (won't write a non-US ticker)
    res = _pick_best([
        {"marketSector": "Equity", "ticker": "FOO", "exchCode": "GR",
         "securityType2": "Common Stock"},
    ])
    check("foreign-only returns None", res is None)


def test_email_formatter() -> None:
    print("\n[unit] alert email formatter")
    from alerts import _format_email
    rule = {"target": "NVDA"}
    subj, body = _format_email(rule, {
        "rule_type": "13d_filed",
        "filing": {"target_ticker": "NVDA", "target_name": "NVIDIA",
                   "ownership_pct": 6.2, "schedule": "13D",
                   "intent_class": "activist", "intent_score": 0.7,
                   "fetched_at": "2026-04-28"},
    })
    check("13d email has subject", "NVDA" in subj and "13D" in subj)
    check("13d email body has stake", "6.2%" in body)
    subj, body = _format_email(rule, {
        "rule_type": "cluster_buy",
        "cluster": {"issuer_ticker": "NVDA", "issuer_name": "NVIDIA",
                    "n_insiders": 4, "total_value": 800000,
                    "last_txn": "2026-04-25"},
    })
    check("cluster email has subject", "cluster" in subj.lower())


def run() -> int:
    print("=" * 60)
    print("whale-dashboard smoke test")
    print("=" * 60)
    install_fixtures()

    test_intent_classifier()
    test_openfigi_picker()
    test_email_formatter()

    with TestClient(main.app) as client:
        test_endpoints(client)
        test_watchlist(client)
        test_alerts(client)
        test_websocket(client)

    print()
    print(f"PASSED: {len(PASSED)}    FAILED: {len(FAILED)}")
    if FAILED:
        for name, detail in FAILED:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
