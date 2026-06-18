"""Deterministic generator for the walk-forward backtest known-answer fixture.

Builds a CALIBRATION -> TEST timeline so the harness can actually demonstrate
narve winning:
  - 10 calibration markets resolving across 2026 (Jan-Aug): the oracle is always
    right, so by the test window it has a long resolved-correct track record and
    its credibility -> ~1.0. The clueless source is always wrong (-> ~0), noisy
    is ~0.5.
  - 5 test markets resolving Sep-Nov: the MARKET PRICE is deliberately MISPRICED
    (leans the wrong way), while the high-credibility oracle is correct. So
    narve's credibility-weighted probability disagrees with the market -> edge ->
    bets in the oracle's direction -> resolves that way -> WINS, while the
    follow-market baseline (which trusts the price) LOSES.
NOT real data. See SCHEMA.md for the record contract.
"""
import json, datetime as dt

ORACLE="@oracle_always_right"; CLUELESS="@clueless_always_wrong"; NOISY="@noisy_coinflip"

def iso(d): return d.strftime("%Y-%m-%d")
def market(mid, q, outcome, resolved, prices, mispriced=False):
    res=dt.date.fromisoformat(resolved)
    # forecasts: oracle right, clueless wrong, noisy ~0.5 — all made ~45 & ~20 days pre-resolution
    f1=res-dt.timedelta(days=45); f2=res-dt.timedelta(days=20)
    oy=0.95 if outcome==1 else 0.05
    cy=0.05 if outcome==1 else 0.95
    forecasts=[]
    for (h,p) in [(ORACLE,oy),(CLUELESS,cy),(NOISY,0.51 if outcome==1 else 0.49)]:
        forecasts.append({"source_handle":h,"predicted_probability":round(p,2),"made_at":iso(f1),"url":f"https://synthetic.test/{h.strip('@')}/{mid}"})
        # second slightly-later call (small jitter) so each source has 2 calls/market
        p2=min(0.98,max(0.02,p+(0.01 if outcome==1 else -0.01)))
        forecasts.append({"source_handle":h,"predicted_probability":round(p2,2),"made_at":iso(f2),"url":f"https://synthetic.test/{h.strip('@')}/{mid}-b"})
    return {"market_id":mid,"question":q,"resolved_outcome":outcome,"resolved_at":resolved,
            "price_timeline":prices,"forecasts":forecasts}

def pl(dates_prices):  # list of (date, yes_price)
    return [{"date":d,"yes_price":p} for d,p in dates_prices]

markets=[]
# ---- 10 CALIBRATION markets, resolving roughly every 3 weeks Jan->Aug 2026 ----
calib_res=["2026-01-20","2026-02-10","2026-03-03","2026-03-24","2026-04-14",
           "2026-05-05","2026-05-26","2026-06-16","2026-07-07","2026-07-28"]
for i,r in enumerate(calib_res):
    outcome = 1 if i%2==0 else 0
    res=dt.date.fromisoformat(r)
    # price timeline: 3 dates in the ~6 weeks before resolution, price near 0.5 (market unsure)
    prices=pl([(iso(res-dt.timedelta(days=40)),0.50),
               (iso(res-dt.timedelta(days=18)),0.52 if outcome==1 else 0.48),
               (iso(res-dt.timedelta(days=3)), 0.55 if outcome==1 else 0.45)])
    markets.append(market(f"synthetic-calib-{i+1:02d}",
                          f"[SYNTHETIC calibration #{i+1}] Outcome {'YES' if outcome else 'NO'} race",
                          outcome, r, prices))

# ---- 5 TEST markets, resolving Sep->Nov 2026, MARKET MISPRICED, oracle right ----
test_specs=[
    ("synthetic-test-senate","[SYNTHETIC] Will Party A hold the Senate (2026)?",1,"2026-09-15",0.38),
    ("synthetic-test-house","[SYNTHETIC] Will Party B flip the House (2026)?",0,"2026-09-30",0.63),
    ("synthetic-test-gov","[SYNTHETIC] Will the incumbent win State X governor (2026)?",1,"2026-10-15",0.41),
    ("synthetic-test-ballot","[SYNTHETIC] Will Ballot Measure 7 pass (2026)?",0,"2026-10-28",0.60),
    ("synthetic-test-presidential-primary","[SYNTHETIC] Will Candidate Z win the primary (2026)?",1,"2026-11-04",0.43),
]
for mid,q,outcome,r,mispx in test_specs:
    res=dt.date.fromisoformat(r)
    # MISPRICED: market sits on the WRONG side of 0.5 vs the true outcome, drifts a little but stays wrong.
    if outcome==1:
        prices=pl([(iso(res-dt.timedelta(days=40)),mispx),
                   (iso(res-dt.timedelta(days=18)),round(mispx+0.02,2)),
                   (iso(res-dt.timedelta(days=3)), round(mispx+0.04,2))])
    else:
        prices=pl([(iso(res-dt.timedelta(days=40)),mispx),
                   (iso(res-dt.timedelta(days=18)),round(mispx-0.02,2)),
                   (iso(res-dt.timedelta(days=3)), round(mispx-0.04,2))])
    markets.append(market(mid,q,outcome,r,prices,mispriced=True))

out={"description":"SYNTHETIC known-answer fixture (CALIBRATION->TEST). NOT real data. "
     "10 calibration markets (Jan-Aug 2026) build @oracle_always_right's track record (always correct) "
     "while @clueless_always_wrong is always wrong and @noisy_coinflip is ~0.5. 5 test markets (Sep-Nov 2026) "
     "are MISPRICED by the market while the now-high-credibility oracle is right, so narve must bet the oracle's "
     "way, WIN, and BEAT the follow-market baseline. See SCHEMA.md.",
     "synthetic":True,"markets":markets}
json.dump(out,open("data/backtest/synthetic_fixture.json","w"),indent=2)
print(f"wrote {len(markets)} markets ({len(calib_res)} calib + {len(test_specs)} test)")
print("resolution dates:", sorted(set(m['resolved_at'] for m in markets)))
