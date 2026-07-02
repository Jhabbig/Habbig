# narve.ai — Financial Model (Costs, Projections, Raise)

*Built July 2, 2026. Companion to `FINANCIAL_MODEL.xlsx` (5 tabs: Assumptions, Costs, Projections, P&L, Raise & Runway — the workbook has live formulas; edit the yellow driver cells and everything recalculates). All prices researched against current official sources; URLs on the Assumptions tab.*

## Headline numbers

| | Value |
|---|---|
| **Bootstrap monthly cost** (today's 3-product fleet on AWS, founder unpaid) | **$161/mo** (~$1,940/yr) |
| **Funded pre-seed monthly burn** (salary, insurance, bookkeeping, marketing) | **$10,148/mo** |
| **One-time setup** (incorporation + SAFE legal + regulatory memo) | **$20,500** |
| **Peak cash need** (24-month base growth case) | **$145,732** |
| **Recommended raise** (peak need + 25% buffer) | **≈ $182,000 → pitch $200,000** |
| **Breakeven** | 16 subscribers (bootstrap) · 964 subscribers (funded plan) |
| **EBITDA turns positive** (funded × base case) | month ~20 |

## Monthly costs — three scenarios (AWS us-east-1 prices)

| Line | Bootstrap | Funded | Full fleet | Notes |
|---|---|---|---|---|
| EC2 | $60.74 | $76.14 | $168.62 | t3.large on-demand / t3.xlarge 1-yr reserved / + m7i-flex.xlarge second box |
| EBS + snapshots + S3 | $5.83 | $11.65 | $23.30 | gp3 $0.08/GB-mo; snapshots $0.05; S3 $0.023 |
| Data transfer out | $0 | $18 | $36 | first 100 GB/mo free, then $0.09/GB |
| Redis | $0 | $0 | $11.68 | on-box until Full fleet (ElastiCache t4g.micro) |
| SES email | $0.50 | $1 | $2 | $0.10 per 1,000 emails |
| Open-Meteo commercial | $33 | $33 | $33 | **required** — free tier is non-commercial; the weather product is sold |
| The Odds API | $0 | $30 | $59 | free 500 credits → 20K → 100K tier (sports revival) |
| Anthropic Claude API | $10 | $50 | $200 | email relay → signal explanations → Truth LLM extraction (Haiku 4.5 @ $1/$5 per MTok, batch −50%) |
| X API (pay-per-use) | $0 | $0 | $300 | only if Truth dashboard revived; $0.005/post read (legacy tiers closed Feb 2026) |
| Alpaca + CoinGecko | $0 | $0 | $228 | free tiers until stock desk needs full SIP ($99) / trackers revived ($129) |
| Domain, Workspace, Sentry, Apple | $13.89 | $48.14 | $55.14 | .ai $82.70/yr; Workspace $7/user; Sentry Team $26; Apple $99/yr |
| Founder salary + payroll tax | $0 | $6,875 | $6,875 | $75k/yr (Pilot 2025 pre-seed median) + 10% |
| Contractor | $0 | $1,500 | $1,500 | part-time planning figure |
| Bookkeeping + insurance + compliance | $37.50 | $505 | $505 | Pilot $99 + GL/E&O/cyber $281 + DE franchise tax/agent/tax-filing amortized $125 |
| Marketing | $0 | $1,000 | $1,000 | planning figure |
| **Total monthly** | **$161** | **$10,148** | **$10,997** | |

**One-time:** Stripe Atlas incorporation $500 · pre-seed SAFE legal review $5,000 (YC docs themselves are free) · **IA/CTA regulatory memo $15,000** (midpoint of $5–25k; strongly recommended before selling trading signals — Advisers Act publisher-exclusion + CTA analysis).

## Projections (24 months)

Blended ARPU **$11.24**/sub/mo from the live `gateway/config.json` prices (40% Market Edge $9.99 + 35% Midterm $14.99 + 25% Weather $7.99). Stripe takes 2.9% + $0.30/charge + 0.7% Billing ≈ 6% of a $9.99 sub.

| Case | Model | Month-24 subs | Month-24 MRR |
|---|---|---|---|
| Conservative | 10 new/mo flat, 6% churn | ~129 | ~$1,450 |
| **Base** | 15 new/mo growing 15%/mo (cap 150), 6% churn | ~1,361 | ~$15,300 |
| Optimistic | 30 new/mo growing 20%/mo (cap 400), 5% churn | ~4,088 | ~$45,950 |

## The raise

On the funded plan × base growth: cumulative cash bottoms out at **−$145,732** (the burn is nearly flat by month 18 as revenue ramps; EBITDA goes positive ~month 20). With a 25% buffer: **≈$182k — a $200k pre-seed on a YC SAFE funds ~24 months to profitability** in the base case. Sensitivity: 12-month runway needs $123k; 18-month needs $146k.

The bootstrap alternative costs ~$1,940/year all-in — 16 paying subscribers cover it. Given that, the raise is really buying **founder salary, marketing, and legal cleanliness**, not servers.

## Caveats

- Projections assume open self-serve signup from month 1; the gateway is currently **invite-token gated** (and fix the Stripe `invoice.paid` renewal gap before charging real subscribers — see `BALANCE_SHEET.md`).
- AWS priced from official us-east-1 price lists (June 2026); Sonnet 5 API pricing is introductory until 2026-08-31.
- Insurance, contractor, marketing, and the regulatory-memo figure are planning estimates (sources and ranges on the Assumptions tab); everything else is a published price.
- Formulas verified by independent recalculation — the workbook's computed results match a from-scratch Python replication of the model.
