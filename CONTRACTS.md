# CONTRACTS.md — narve Terminal inter-component contracts

Changes to any contract here: bump the version, both owners sign off in this
file, THEN code. The JSON shapes are normative; prose around them is not.

---

## Contract 1 — ExplainRequest / ExplainReport (prediction ⇄ why engine)

**Version:** 1.0.0
**Owners:** Julian (prediction side — produces ExplainRequest) ·
Sho/Claude (why engine — consumes it, returns ExplainReport)
**Sign-off:** why-engine side ✅ 2026-08-18 · prediction side ⬜ pending

The prediction side computes the headline probability. The why engine NEVER
recomputes or overrides it — it explains it.

### ExplainRequest (input to `narve-why explain`)

```json
{
  "question_id": "midterm-house-gop-2026",
  "question_text": "Do Republicans win the U.S. House majority?",
  "probability": 0.62,
  "as_of": "2026-07-01T09:00:00Z",
  "model_outputs": [
    {"model_id": "race_model", "source_id": "race_model", "p": 0.64,
     "weight": 0.34, "inputs_ref": ["pred:1", "poll:az-0612"]},
    {"model_id": "state_polls", "source_id": "state_polls", "p": 0.61,
     "weight": 0.31, "inputs_ref": ["poll:mi-0609"]}
  ],
  "market_snapshots": [
    {"venue": "kalshi", "yes_price": 0.55, "liquidity": 120000,
     "captured_at": "2026-07-01T08:40:00Z"}
  ]
}
```

Field rules (validated loudly, exact field path in every error):
- `probability`, every `p`, every `yes_price` ∈ [0,1].
- `weight` ≥ 0; weights need not sum to 1 (they are normalised downstream).
- `model_outputs` non-empty; `inputs_ref` non-empty per entry (empty evidence
  is a contract violation — the why engine refuses, it does not invent).
- `market_snapshots` may be empty (then `market_gap` is null and the prose
  says there is no market to compare).
- Unknown extra fields are ignored (forward compatibility).
- Timestamps ISO-8601 UTC.

### Source credibility state (NOT in the request — by design)

Credibility is the why engine's own context, pulled from the credibility
engine (terminal sidecar DB, Beta(2,2) posterior, resolved-only scoring):
`narve-why explain --in payload.json --db ~/.narve-terminal/terminal.db`.
Standalone/demo mode uses a bundled fixture state
(`fixtures/sources_state.json`, same numbers as the sample DB). Julian never
sends credibility; his side doesn't own it.

### ExplainReport (output)

```json
{
  "question_id": "...",
  "probability": 0.62,
  "market_gap": {"venue": "kalshi", "gap_pts": 7.0, "read": "market looks cheap"},
  "drivers": [
    {"tag": "polling", "label": "State polling favors GOP in the seats that decide it",
     "direction": "up", "weight": 0.31, "evidence_refs": ["poll:mi-0609"]}
  ],
  "sources": [
    {"source_id": "race_model", "credibility": 0.8125, "n_resolved": 1,
     "stance_p": 0.64, "contribution": 0.34}
  ],
  "conflicts": [
    {"note": "state_polls (0.61) and macro_model (0.34) are 27 pts apart",
     "evidence_refs": ["pred:9"]}
  ],
  "would_change_it": ["New national generic-ballot print"],
  "prose": "62% vs a 55% market. ...",
  "confidence_note": "HIGH — 4 sources above 0.75 credibility, agreeing within 5 pts"
}
```

Guarantees the why engine makes:
- Deterministic: same request + same credibility state → byte-identical
  structured output (prose byte-identical too unless the optional LLM polish
  is enabled; polish may reword, never add claims).
- `driver.tag` ∈ {governance, funding, polling, economy, legal, incident,
  momentum, market_structure}. `direction` ∈ {up, down}. Driver `weight` =
  share of explained movement, descending, Σ ≤ 1.
- `market_gap.gap_pts` = (probability − yes_price) × 100, one decimal; venue =
  deepest liquidity, tie → latest capture. `read` ∈ {"market looks cheap",
  "market looks rich", "in line with market"} (cheap ⇔ gap ≥ +2.0 pts,
  rich ⇔ ≤ −2.0).
- Every driver / source / conflict carries non-empty `evidence_refs` (row ids
  from the ingest layer) — traceability is a hard invariant.
- Thin context is stated, never padded: fewer than 2 sources, or zero resolved
  calls across all contributing sources, forces a "THIN — ..." confidence_note.

Seam (documented, v1.1 candidate): `market_snapshots` may grow to a per-venue
price HISTORY array; `market_gap` would then also report direction-of-drift.
Until then a single latest snapshot per venue is the contract.
