# narve.ai — Financial Model v2 (Costs, Projections, Raise)

*Rebuilt July 2, 2026 — **v2: founder salary removed; AWS GPU compute for training our own models is now the main cost.** Companion to `FINANCIAL_MODEL.xlsx` (live formulas; edit the yellow driver cells — GPU hours/rates included — and everything recalculates). All prices verified against official sources; URLs on the Assumptions tab.*

## Headline numbers

| | Value |
|---|---|
| **Bootstrap** (trimmed 3-product fleet, no training) | **$161/mo** (~$1,940/yr) |
| **ML fine-tune plan** (main plan — the P&L runs on this) | **$2,727/mo** — 57% of it AWS; GPU training $1,454/mo is the single biggest line |
| **ML scale plan** (H100 Capacity Blocks) | **$12,763/mo** — 75% AWS |
| **One-time setup** (incorporation + SAFE legal + regulatory memo) | **$20,500** |
| **Peak cash need** (fine-tune × base growth) | **$35,638** |
| **Recommended raise** (peak + 25% buffer) | **≈ $45,000 → pitch $50–60k** (or ~$313k if committing to H100 scale training from day 1, ignoring revenue) |
| **Breakeven** | 16 subscribers (bootstrap) · 259 subscribers (fine-tune plan) |
| **EBITDA turns positive** (fine-tune × base case) | month ~11; cumulative cash back above zero before month 24 (+$45k at M24) |

## AWS GPU training prices (us-east-1, verified from the official price list, 2026-07-02)

| Instance | GPUs | On-demand | Spot (approx) | Notes |
|---|---|---|---|---|
| g5.xlarge | 1× A10G 24 GB | $1.006/hr | ~$0.44 | cheapest dev GPU |
| g6.xlarge | 1× L4 24 GB | $0.805/hr | ~$0.41 | inference-oriented |
| **g6e.xlarge** | 1× L40S 48 GB | **$1.861/hr** | ~$0.93–1.47 | fine-tuning workhorse (fits 7–13B LoRA); 1-yr reserved $1.172/hr (−37%) |
| g6e.12xlarge | 4× L40S | $10.49/hr | ~$5.1 | multi-GPU fine-tunes |
| p4d.24xlarge | 8× A100 40 GB | $21.96/hr | ~$9.7 (±30%) | post-2025 price cut |
| p5.48xlarge | 8× H100 80 GB | $55.04/hr | ~$24 (volatile) | **Capacity Blocks: $5.191/GPU-hr = $41.53/hr** (after July 1, 2026 ~20% hike; floats with demand) |
| trn1.32xlarge | 16× Trainium | $21.50/hr | ~$8.4 | <5% spot interruption — cheapest reliable big-training if the stack can use Neuron |

Spot on the g-family has **>20% interruption rates** — fine for short fine-tunes with checkpointing, bad for long runs. p5/trn1 spot interrupts <5%.

## Monthly costs — three scenarios

| Section | Bootstrap | ML fine-tune | ML scale |
|---|---|---|---|
| AWS — serving (t3 app box, EBS, S3, egress, SES) | $67 | $107 | $107 |
| **AWS — model training (GPU)** | $0 | **$1,454** | **$9,491** |
| — single-GPU dev/fine-tune (g6e.xlarge, 300 h/mo) | — | $558 | $558 |
| — multi-GPU runs (g6e.12xlarge, 80 h/mo) | — | $839 | $839 |
| — H100 Capacity-Block week (168 h/mo) | — | — | $6,977 |
| — A100 spot experiments (100 h/mo) | — | — | $970 |
| — training storage + transfer | — | $57 | $146 |
| Data & AI APIs (Open-Meteo commercial, Odds API, Claude) | $43 | $113 | $113 |
| SaaS & tooling (domain, Workspace, Sentry, Apple) | $14 | $48 | $48 |
| People & professional (**no founder salary**; bookkeeping, insurance, compliance; ML contractor at scale) | $38 | $505 | $2,005 |
| Marketing | $0 | $500 | $1,000 |
| **Total monthly** | **$161** | **$2,727** | **$12,763** |
| AWS share | 42% | **57%** | **75%** |

GPU hours and rates are editable drivers on the Assumptions tab — the Costs and P&L tabs recalculate from them.

**One-time ($20,500):** Stripe Atlas $500 · SAFE legal review $5,000 · IA/CTA regulatory memo $15,000 (still recommended before selling trading signals).

## Projections (unchanged from v1)

Blended ARPU $11.24/sub/mo from live `gateway/config.json` prices; Stripe takes ≈6% of a $9.99 sub. Month-24 MRR: ~$1,450 conservative / ~$15,300 base / ~$45,950 optimistic.

## The raise

Without a salary, the model is dramatically lighter: on the fine-tune plan × base growth, cumulative cash bottoms out at **−$35,638** (month ~11, when EBITDA turns positive), and is back above zero (+$45k) by month 24. With the 25% buffer: **≈$45k — a $50–60k pre-seed SAFE covers the whole plan**, and most of it is literally GPU hours and the legal one-times.

If you commit to **H100-scale training from day 1**, the crude upper bound (18 months of scale burn + one-times + buffer, giving no credit for revenue) is **≈$313k**.

Middle path worth considering: raise ~$60k, run the fine-tune plan, and buy individual H100 Capacity-Block weeks (~$7k each) only when an experiment earns it.

## Caveats

- Projections assume open self-serve signup from month 1; the gateway is currently invite-token gated, and the Stripe `invoice.paid` renewal gap should be fixed before charging subscribers (see `BALANCE_SHEET.md`).
- GPU spot prices are ±30% volatile; Capacity-Block rates rose ~15% in Jan 2026 and ~20% on July 1, 2026 and float with demand — re-verify before committing.
- No salary means the founder is unpaid indefinitely — investors may push back; add it back as a driver if needed.
- Formulas verified by independent recalculation (workbook results match a from-scratch replication).
