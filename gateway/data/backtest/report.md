# narve walk-forward backtest — proof report

**Thesis under test:** credibility-scored social-media forecasters beat the prediction market, with **zero lookahead**.

- **Generated:** 2026-06-18 08:29 UTC
- **Methodologies:** strict_two_window, bayesian (both cold-start methods reported side by side)
- **Baselines:** always-follow-market, flat/unweighted ensemble (scored by the same engine on the same markets/dates/stakes)

> **Sample size (N):** 14 distinct markets; 21 total bets logged across methodologies. Small-N demo — every bet is listed below and is individually auditable. This number does not imply more data than exists.

## Cold-start methodology: strict_two_window

N = **7 bets** on **7 markets**. Starting bankroll $10,000.

### narve vs. baselines

| Metric | narve (credibility-weighted) | Follow market | Flat ensemble |
| --- | --- | --- | --- |
| ROI | +680.70% | +0.00% | +680.70% |
| Win rate | 100.0% | 0.0% | 100.0% |
| Sharpe | 4.8657 | 0.0000 | 4.8657 |
| Max drawdown | 0.0% | 0.0% | 0.0% |
| Bets placed | 7 | 0 | 7 |
| Final bankroll | $78,070 | $10,000 | $78,070 |

_Follow-market believes the price exactly, so it has no edge and never bets (0% by construction — the null hypothesis). The flat ensemble bets the same markets/stakes as narve but equal-weights every forecaster, ignoring credibility._

### Per-bet detail (auditable, bet-by-bet)

| # | Date | Market | narve P | Mkt price | Edge | Side | Stake | Outcome | P&L | Bankroll |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2026-06-17 | [SYNTHETIC calibration #9] Outcome YES race | 65.8% | 50.0% | +15.8% | YES | $2,500 | win | $2,500 | $12,500 |
| 2 | 2026-07-08 | [SYNTHETIC calibration #10] Outcome NO race | 33.7% | 50.0% | +16.3% | NO | $3,125 | win | $3,125 | $15,625 |
| 3 | 2026-08-26 | [SYNTHETIC] Will Party A hold the Senate (2026)? | 66.8% | 38.0% | +28.8% | YES | $3,906 | win | $6,373 | $21,998 |
| 4 | 2026-09-10 | [SYNTHETIC] Will Party B flip the House (2026)? | 33.2% | 63.0% | +29.8% | NO | $5,500 | win | $9,364 | $31,363 |
| 5 | 2026-09-25 | [SYNTHETIC] Will the incumbent win State X gover… | 67.2% | 41.0% | +26.2% | YES | $7,841 | win | $11,283 | $42,645 |
| 6 | 2026-10-08 | [SYNTHETIC] Will Ballot Measure 7 pass (2026)? | 32.4% | 60.0% | +27.6% | NO | $10,661 | win | $15,992 | $58,637 |
| 7 | 2026-10-15 | [SYNTHETIC] Will Candidate Z win the primary (20… | 67.6% | 43.0% | +24.6% | YES | $14,659 | win | $19,432 | $78,070 |

### As-of credibility inputs per bet

**Bet 1 — 2026-06-17 — synthetic-calib-09**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8077 | 95.0% |
| @clueless_always_wrong | 0.1923 | 5.0% |
| @noisy_coinflip | 0.8077 | 51.0% |
| **narve weighted P(YES)** | | **65.8%** (market 50.0%, edge +15.8%) |

**Bet 2 — 2026-07-08 — synthetic-calib-10**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8214 | 5.0% |
| @clueless_always_wrong | 0.1786 | 95.0% |
| @noisy_coinflip | 0.8214 | 49.0% |
| **narve weighted P(YES)** | | **33.7%** (market 50.0%, edge +16.3%) |

**Bet 3 — 2026-08-26 — synthetic-test-senate**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8333 | 95.0% |
| @clueless_always_wrong | 0.1667 | 5.0% |
| @noisy_coinflip | 0.8333 | 51.0% |
| **narve weighted P(YES)** | | **66.8%** (market 38.0%, edge +28.8%) |

**Bet 4 — 2026-09-10 — synthetic-test-house**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8333 | 5.0% |
| @clueless_always_wrong | 0.1667 | 95.0% |
| @noisy_coinflip | 0.8333 | 49.0% |
| **narve weighted P(YES)** | | **33.2%** (market 63.0%, edge +29.8%) |

**Bet 5 — 2026-09-25 — synthetic-test-gov**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8438 | 95.0% |
| @clueless_always_wrong | 0.1562 | 5.0% |
| @noisy_coinflip | 0.8438 | 51.0% |
| **narve weighted P(YES)** | | **67.2%** (market 41.0%, edge +26.2%) |

**Bet 6 — 2026-10-08 — synthetic-test-ballot**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8529 | 5.0% |
| @clueless_always_wrong | 0.1471 | 95.0% |
| @noisy_coinflip | 0.8529 | 49.0% |
| **narve weighted P(YES)** | | **32.4%** (market 60.0%, edge +27.6%) |

**Bet 7 — 2026-10-15 — synthetic-test-presidential-primary**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8529 | 95.0% |
| @clueless_always_wrong | 0.1471 | 5.0% |
| @noisy_coinflip | 0.8529 | 51.0% |
| **narve weighted P(YES)** | | **67.6%** (market 43.0%, edge +24.6%) |

## Cold-start methodology: bayesian

N = **14 bets** on **14 markets**. Starting bankroll $10,000.

### narve vs. baselines

| Metric | narve (credibility-weighted) | Follow market | Flat ensemble |
| --- | --- | --- | --- |
| ROI | +2832.81% | +0.00% | +2832.81% |
| Win rate | 100.0% | 0.0% | 100.0% |
| Sharpe | 4.3653 | 0.0000 | 4.3653 |
| Max drawdown | 0.0% | 0.0% | 0.0% |
| Bets placed | 14 | 0 | 14 |
| Final bankroll | $293,281 | $10,000 | $293,281 |

_Follow-market believes the price exactly, so it has no edge and never bets (0% by construction — the null hypothesis). The flat ensemble bets the same markets/stakes as narve but equal-weights every forecaster, ignoring credibility._

### Per-bet detail (auditable, bet-by-bet)

| # | Date | Market | narve P | Mkt price | Edge | Side | Stake | Outcome | P&L | Bankroll |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2026-01-21 | [SYNTHETIC calibration #2] Outcome NO race | 44.9% | 50.0% | +5.1% | NO | $1,021 | win | $1,021 | $11,021 |
| 2 | 2026-02-11 | [SYNTHETIC calibration #3] Outcome YES race | 58.2% | 50.0% | +8.2% | YES | $1,811 | win | $1,811 | $12,832 |
| 3 | 2026-03-04 | [SYNTHETIC calibration #4] Outcome NO race | 39.6% | 50.0% | +10.4% | NO | $2,671 | win | $2,671 | $15,503 |
| 4 | 2026-03-25 | [SYNTHETIC calibration #5] Outcome YES race | 62.0% | 50.0% | +12.0% | YES | $3,731 | win | $3,731 | $19,234 |
| 5 | 2026-04-15 | [SYNTHETIC calibration #6] Outcome NO race | 36.7% | 50.0% | +13.3% | NO | $4,809 | win | $4,809 | $24,043 |
| 6 | 2026-05-06 | [SYNTHETIC calibration #7] Outcome YES race | 64.3% | 50.0% | +14.3% | YES | $6,011 | win | $6,011 | $30,053 |
| 7 | 2026-05-27 | [SYNTHETIC calibration #8] Outcome NO race | 34.9% | 50.0% | +15.1% | NO | $7,513 | win | $7,513 | $37,567 |
| 8 | 2026-06-17 | [SYNTHETIC calibration #9] Outcome YES race | 65.8% | 50.0% | +15.8% | YES | $9,392 | win | $9,392 | $46,958 |
| 9 | 2026-07-08 | [SYNTHETIC calibration #10] Outcome NO race | 33.7% | 50.0% | +16.3% | NO | $11,740 | win | $11,740 | $58,698 |
| 10 | 2026-08-26 | [SYNTHETIC] Will Party A hold the Senate (2026)? | 66.8% | 38.0% | +28.8% | YES | $14,674 | win | $23,943 | $82,641 |
| 11 | 2026-09-10 | [SYNTHETIC] Will Party B flip the House (2026)? | 33.2% | 63.0% | +29.8% | NO | $20,660 | win | $35,178 | $117,819 |
| 12 | 2026-09-25 | [SYNTHETIC] Will the incumbent win State X gover… | 67.2% | 41.0% | +26.2% | YES | $29,455 | win | $42,386 | $160,204 |
| 13 | 2026-10-08 | [SYNTHETIC] Will Ballot Measure 7 pass (2026)? | 32.4% | 60.0% | +27.6% | NO | $40,051 | win | $60,077 | $220,281 |
| 14 | 2026-10-15 | [SYNTHETIC] Will Candidate Z win the primary (20… | 67.6% | 43.0% | +24.6% | YES | $55,070 | win | $73,000 | $293,281 |

### As-of credibility inputs per bet

**Bet 1 — 2026-01-21 — synthetic-calib-02**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.5833 | 5.0% |
| @clueless_always_wrong | 0.4167 | 95.0% |
| @noisy_coinflip | 0.5833 | 49.0% |
| **narve weighted P(YES)** | | **44.9%** (market 50.0%, edge +5.1%) |

**Bet 2 — 2026-02-11 — synthetic-calib-03**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.6429 | 95.0% |
| @clueless_always_wrong | 0.3571 | 5.0% |
| @noisy_coinflip | 0.6429 | 51.0% |
| **narve weighted P(YES)** | | **58.2%** (market 50.0%, edge +8.2%) |

**Bet 3 — 2026-03-04 — synthetic-calib-04**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.6875 | 5.0% |
| @clueless_always_wrong | 0.3125 | 95.0% |
| @noisy_coinflip | 0.6875 | 49.0% |
| **narve weighted P(YES)** | | **39.6%** (market 50.0%, edge +10.4%) |

**Bet 4 — 2026-03-25 — synthetic-calib-05**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.7222 | 95.0% |
| @clueless_always_wrong | 0.2778 | 5.0% |
| @noisy_coinflip | 0.7222 | 51.0% |
| **narve weighted P(YES)** | | **62.0%** (market 50.0%, edge +12.0%) |

**Bet 5 — 2026-04-15 — synthetic-calib-06**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.7500 | 5.0% |
| @clueless_always_wrong | 0.2500 | 95.0% |
| @noisy_coinflip | 0.7500 | 49.0% |
| **narve weighted P(YES)** | | **36.7%** (market 50.0%, edge +13.3%) |

**Bet 6 — 2026-05-06 — synthetic-calib-07**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.7727 | 95.0% |
| @clueless_always_wrong | 0.2273 | 5.0% |
| @noisy_coinflip | 0.7727 | 51.0% |
| **narve weighted P(YES)** | | **64.3%** (market 50.0%, edge +14.3%) |

**Bet 7 — 2026-05-27 — synthetic-calib-08**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.7917 | 5.0% |
| @clueless_always_wrong | 0.2083 | 95.0% |
| @noisy_coinflip | 0.7917 | 49.0% |
| **narve weighted P(YES)** | | **34.9%** (market 50.0%, edge +15.1%) |

**Bet 8 — 2026-06-17 — synthetic-calib-09**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8077 | 95.0% |
| @clueless_always_wrong | 0.1923 | 5.0% |
| @noisy_coinflip | 0.8077 | 51.0% |
| **narve weighted P(YES)** | | **65.8%** (market 50.0%, edge +15.8%) |

**Bet 9 — 2026-07-08 — synthetic-calib-10**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8214 | 5.0% |
| @clueless_always_wrong | 0.1786 | 95.0% |
| @noisy_coinflip | 0.8214 | 49.0% |
| **narve weighted P(YES)** | | **33.7%** (market 50.0%, edge +16.3%) |

**Bet 10 — 2026-08-26 — synthetic-test-senate**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8333 | 95.0% |
| @clueless_always_wrong | 0.1667 | 5.0% |
| @noisy_coinflip | 0.8333 | 51.0% |
| **narve weighted P(YES)** | | **66.8%** (market 38.0%, edge +28.8%) |

**Bet 11 — 2026-09-10 — synthetic-test-house**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8333 | 5.0% |
| @clueless_always_wrong | 0.1667 | 95.0% |
| @noisy_coinflip | 0.8333 | 49.0% |
| **narve weighted P(YES)** | | **33.2%** (market 63.0%, edge +29.8%) |

**Bet 12 — 2026-09-25 — synthetic-test-gov**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8438 | 95.0% |
| @clueless_always_wrong | 0.1562 | 5.0% |
| @noisy_coinflip | 0.8438 | 51.0% |
| **narve weighted P(YES)** | | **67.2%** (market 41.0%, edge +26.2%) |

**Bet 13 — 2026-10-08 — synthetic-test-ballot**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8529 | 5.0% |
| @clueless_always_wrong | 0.1471 | 95.0% |
| @noisy_coinflip | 0.8529 | 49.0% |
| **narve weighted P(YES)** | | **32.4%** (market 60.0%, edge +27.6%) |

**Bet 14 — 2026-10-15 — synthetic-test-presidential-primary**

| Source | as-of credibility | stated P(YES) |
| --- | --- | --- |
| @oracle_always_right | 0.8529 | 95.0% |
| @clueless_always_wrong | 0.1471 | 5.0% |
| @noisy_coinflip | 0.8529 | 51.0% |
| **narve weighted P(YES)** | | **67.6%** (market 43.0%, edge +24.6%) |

---

_Integrity: no-lookahead is enforced at dataset load (`gateway/backtest_dataset.py`) and again per-decision in the replay harness. Baselines are scored by the same engine code path as narve (`gateway/backtest.py`). This is a proof document, not a designed page._
