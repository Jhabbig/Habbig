# narve — Polymarket trader credibility leaderboard (REAL data)

**Source:** Polymarket Goldsky subgraph `marketProfit` + `condition` — on-chain
realized profit per wallet across **resolved, decisive** markets. No directional
reconstruction, no proxy: profit and outcomes are on-chain facts.

**Sample:** 8,000 P&L rows pulled, 7,308 on resolved+decisive markets, **302 wallets** with ≥5 resolved markets.

> **Limitation (stated):** the subgraph returns rows in id order, so this is a
> SAMPLE of traders, not the global top. A full ranking needs deep pagination.
> This proves WHO is sharp (realized profit) — NOT per-market YES/NO accuracy,
> which needs the raw fills (enrichedOrderFilled) and is a separate next step.

## Top 25 by realized profit

| # | wallet | realized profit | resolved markets | profitable | hit-rate |
| - | ------ | --------------- | ---------------- | ---------- | -------- |
| 1 | `0x000aa2ca95bc32…` | $8,552 | 5 | 3 | 60% |
| 2 | `0x00100d838cb5dc…` | $4,955 | 15 | 4 | 27% |
| 3 | `0x000e82e39c383d…` | $4,635 | 13 | 5 | 38% |
| 4 | `0x000da5c4606f0c…` | $137 | 21 | 7 | 33% |
| 5 | `0x00064138bab1d8…` | $58 | 19 | 3 | 16% |
| 6 | `0x0002d8afff6877…` | $10 | 8 | 2 | 25% |
| 7 | `0x0000e78a359eb7…` | $6 | 14 | 2 | 14% |
| 8 | `0x000ba710a7a20c…` | $6 | 6 | 1 | 17% |
| 9 | `0x00124e36921de3…` | $2 | 20 | 10 | 50% |
| 10 | `0x0001c98790282f…` | $1 | 11 | 3 | 27% |
| 11 | `0x00046bf835ace9…` | $1 | 5 | 2 | 40% |
| 12 | `0x0010f845902741…` | $0 | 9 | 1 | 11% |
| 13 | `0x0002f066b474fb…` | $0 | 5 | 1 | 20% |
| 14 | `0x0010b52a869451…` | $0 | 12 | 3 | 25% |
| 15 | `0x0009f32b161c3e…` | $0 | 7 | 0 | 0% |
| 16 | `0x000a0384e6cced…` | $0 | 16 | 0 | 0% |
| 17 | `0x000100c19b3bf2…` | $-0 | 6 | 1 | 17% |
| 18 | `0x0007961e874e09…` | $-0 | 5 | 0 | 0% |
| 19 | `0x000e668f97e811…` | $-0 | 5 | 0 | 0% |
| 20 | `0x0002ac172afa5b…` | $-0 | 5 | 0 | 0% |
| 21 | `0x000621a2b04833…` | $-0 | 5 | 0 | 0% |
| 22 | `0x000af49d51a628…` | $-0 | 5 | 0 | 0% |
| 23 | `0x000097db776850…` | $-0 | 5 | 0 | 0% |
| 24 | `0x0002e181b352ab…` | $-0 | 6 | 3 | 50% |
| 25 | `0x00036fe0876ed2…` | $-0 | 5 | 0 | 0% |
