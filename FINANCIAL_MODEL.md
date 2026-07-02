# narve.ai — Financial Model v3 (banker-grade operating model)

*Rebuilt July 2, 2026. Companion to `FINANCIAL_MODEL.xlsx` — a 36-month, three-statement operating model in investment-banking format. All third-party prices verified against official sources as of 2026-07-02 (sources on the Pricing Data tab).*

## What's in the workbook

| Tab | Contents |
|---|---|
| **Cover** | Company summary, contents, formatting conventions, key model choices |
| **Drivers** | Every input in one place — blue-on-yellow cells. Includes a **scenario switch** (1 Bear / 2 Base / 3 Bull) driving growth/churn via INDEX, and a **training-plan toggle** (1 Fine-tune / 2 Scale) that switches H100 Capacity Blocks, A100 spot, and the ML contractor on/off. **Founder salary is a driver, default $0** — set it to e.g. 60000 when funded and the whole model recalculates. |
| **Pricing Data** | The sourced price reference (AWS GPU/serving, APIs, SaaS, professional) |
| **Rev Build** | Bottoms-up revenue: gross-adds engine → per-product subscriber waterfalls (Weather $7.99 / Market Edge $9.99 / Midterm $14.99, mix 25/40/35) → MRR → ARR |
| **Opex** | Cost schedule: COGS (Stripe + serving infra + APIs), R&D (GPU training), S&M, G&A, one-times |
| **Income Statement** | Monthly P&L to net income (D&A $0 — rented compute; taxes $0 — NOLs) |
| **Cash Flow & BS** | Cash bridge with SAFE proceeds in M1; mini balance sheet (cash = SAFE liability + retained earnings) with a **tie-out check row** |
| **Annual & KPIs** | FY1–FY3 rollups + SaaS metrics: LTV/CAC, CAC payback, burn multiple, Rule of 40, peak funding need, recommended raise |
| **Sensitivity** | Peak-funding-need and M24-MRR grids across churn (4–8%) × adds growth (10–20%), for both training plans |
| **Cap Table & Valuation** | Post-money SAFE conversion math (live), founder-ownership grid (raise × cap), forward-ARR multiple valuation |
| **Checks** | BS tie-out, mix = 100%, minimum-cash flag, margin sanity — all read OK |

Banker conventions throughout: blue = hardcoded input, black = calculation, green = cross-sheet link; negatives in red parentheses; monthly columns M1–M36 (Aug-26 → Jul-29).

## Base case at a glance (Base demand × Fine-tune plan × $0 salary × $60k SAFE)

| | FY1 | M24 | M36 |
|---|---|---|---|
| Subscribers | 348 (end) | 1,361 | 1,958 |
| Revenue / MRR | $16.3k (FY) | $15.3k/mo | $22.0k/mo |
| ARR | $39.9k (end) | $184k | $264k |
| EBITDA | −$35.2k (FY, incl. $20.5k one-times) | +$11.6k/mo | positive 26 of 36 months |
| Ending cash | $24.8k | $105k | $290k |

- **Gross margin ~92%** at M24 (COGS = Stripe ≈6% + serving infra + API licenses).
- **Peak funding need $35.6k** → recommended raise **≈$44.5k → pitch $50–60k**. With the $60k SAFE modeled, minimum cash is +$24.4k — the plan is fully funded with margin.
- **Burn multiple FY1 ≈ 0.9×** (net burn / net new ARR) — under 1× is considered excellent.
- LTV/CAC computes at ~52× — a flag that the $500/mo marketing input is doing very little work; real CAC will be higher once acquisition is paid.
- Scale-training plan (H100 Capacity-Block week every month): peak need jumps to ~$250–320k range depending on churn/growth (see the Sensitivity tab's third grid).

## Cap table (defaults: $60k SAFE at $2M post-money cap)

SAFE converts to **3.0%**; founder keeps **97.0%** of 10M shares. The ownership grid covers raises $50k–$250k × caps $1M–$5M (e.g. $250k at $1M cap = 75% founder — don't do that; $100k at $2M = 95%).

## Valuation context

Forward-ARR multiples (live, from Rev Build): at M24 base ARR $184k → $551k–$1.8M at 3–10×; at M36 ARR $264k → $792k–$2.6M. Small prosumer-signals SaaS typically trades 3–7× ARR — the $2M SAFE cap is defensible against the M24–M36 base trajectory plus the trained-model IP.

## Caveats

- Sensitivity grids are computed at build time from the identical model logic (openpyxl can't emit Excel data tables); the Drivers switches cover live what-ifs.
- Projections assume open self-serve signup from month 1 (gateway is currently invite-gated) and require fixing the Stripe `invoice.paid` renewal gap (`BALANCE_SHEET.md`).
- GPU spot ±30%; Capacity-Block rates float (hiked ~20% July 1, 2026). Taxes/D&A simplifications noted on the Cover.
- Formula integrity machine-verified: balance sheet ties to <1e-10 across all 36 months; all checks read OK.
