# narve.ai — Financial Model v4 (realistic operating model)

*Rebuilt July 2, 2026. Companion to `FINANCIAL_MODEL.xlsx` — 36-month, three-statement operating model. All third-party prices verified against official sources as of 2026-07-02 (Pricing Data tab). Formula integrity machine-verified: balance sheet ties to <1e-10 in every month, FY rollups tie to monthly sums, all Checks read OK.*

## What v4 fixes about realism

| v3 assumed | v4 models |
|---|---|
| Growth was free ($500/mo marketing → 52× LTV/CAC) | **Adds = invite trickle + organic ramp + marketing ÷ CAC** (Base CAC $60; Bear $90 / Bull $35). LTV/CAC lands at an honest **2.6×**, CAC payback **6.5 months** |
| Revenue from day 1 | **Public launch delayed to M3** (open the invite gate, fix Stripe renewals, get the regulatory memo first); pre-launch only a 4/mo invite trickle |
| Uniform churn | **First-month activation drop** (15% of new monthly subs) + ongoing 6%/mo (incl. failed payments) + **annual non-renewal 35%** at month 12 |
| Monthly billing only | **20% of adds prepay annually** (~2 months free) → billings ≠ revenue, a **deferred-revenue liability** on the BS, and CFO = NI + ΔDR |
| Training but free serving | **GPU inference** (g6.xlarge reserved, $382/mo) from launch + per-subscriber support tooling in COGS |
| Static KPI table | **Dashboard tab with live Excel charts** (revenue vs billings, cash, subscriber pools, EBITDA) that follow the Drivers switches |

Structure retained from v3: Bear/Base/Bull scenario switch, Fine-tune/Scale training toggle, founder salary as a driver (default $0), SAFE cap table, forward-ARR valuation, sensitivity grids (now churn × CAC), Checks tab.

## Base case (Base demand × Fine-tune plan × $0 salary × $125k SAFE, launch M3)

| | FY1 | FY2 | FY3 |
|---|---|---|---|
| Revenue (recognized) | $17.1k | $86.7k | $196.7k |
| Ending ARR | $39.9k | $134.6k | $254.6k |
| Ending subscribers | 311 | 1,051 | 1,993 |
| EBITDA | −$51.3k (incl. $20.5k one-times) | −$0.8k (≈breakeven) | +$90.9k |
| Ending cash (with $125k SAFE) | $79.2k | $90.9k | $197.9k |

- **EBITDA turns positive month 21**; gross margin ramps 74% (M12) → 87% (M24) → 90% (M36) as fixed serving costs amortize.
- **Peak funding need $48.2k** → recommended raise **$60.3k with the 25% buffer**. The modeled **$125k SAFE** keeps minimum cash at +$76.8k — deliberate headroom, because the CAC and churn assumptions are the untested part of the plan (see sensitivity).
- Deferred revenue builds to **$34k by M36** — real prepay float that helps cash but is a liability, not income.
- Marketing becomes the biggest cost line at scale (AWS is 23% of M24 costs on the fine-tune plan; flip the training toggle to Scale and AWS dominates again). That's the honest trade: **growth costs money; training costs are the choice.**

## Sensitivity (churn × paid CAC, static grids from identical logic)

Peak funding need on the fine-tune plan spans roughly **$40k (4.5% churn / $30 CAC) to $75k+ (9% churn / $120 CAC)**; the Scale plan (H100 Capacity-Block week monthly) adds ~$9.5k/mo of R&D and pushes peak need into the $250–350k range. Grids are on the Sensitivity tab; the highlighted cell is the base case.

## Cap table & valuation (defaults: $125k SAFE at $2M post-money cap)

SAFE converts to **6.25%**; founder keeps **93.75%**. Ownership grid covers $75k–$300k raises × $1M–$5M caps. Forward-ARR valuation (live): M24 ARR $134.6k → $404k–$1.35M at 3–10×; M36 ARR $254.6k → $764k–$2.5M. A $2M cap is defensible mid-range against the M24–M36 trajectory plus trained-model IP.

## Caveats

- CAC ($60 base) and churn (6%) are the two assumptions with no data behind them yet — everything else is a researched price or a config.json fact. Instrument them from the first month of launch.
- Stripe fees are expensed as incurred (annual-prepay fees hit at purchase, slightly conservative vs deferring them).
- Sensitivity grids are computed at build time (openpyxl can't emit native data tables); Drivers switches cover live what-ifs.
- GPU spot ±30%; Capacity-Block rates float (hiked ~20% July 1, 2026). Taxes $0 (NOLs), no D&A (rented compute).
- Prerequisites baked into the launch delay: open the invite gate, fix the Stripe `invoice.paid` renewal gap (`BALANCE_SHEET.md`), obtain the IA/CTA memo.
