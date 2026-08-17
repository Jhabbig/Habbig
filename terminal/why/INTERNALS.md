# narve_why — internal build contract (for the parallel builders)

Location: `terminal/why/`. Public contract lives in repo-root `CONTRACTS.md`
(v1.0.0) — read it first; it wins over this file on any clash.

## Layout
```
terminal/why/
  pyproject.toml            # package narve-why, console script narve-why
  narve_why/
    __init__.py  __main__.py          # python3.11 -m narve_why also works
    schemas.py               # dataclasses + validate() with exact-path errors
    credstate.py             # credibility state: fixture json OR sidecar db
    drivers.py               # deterministic driver extraction
    conflicts.py             # disagreement surfacing
    would_change.py          # stalest-highest-weight data points
    prose.py                 # template prose + banned-phrase guard + LLM seam
    report.py                # assemble ExplainReport (the only orchestrator)
    cli.py                   # narve-why explain --in X [--db Y] [--format json|text]
  fixtures/
    sources_state.json       # {source_id: {alpha, beta, n_resolved}} — MUST
                             # match terminal sample DB numbers exactly
                             # (race_model 13/3, poll_aggregator 19/6,
                             #  state_polls 12/5, macro_model 10/3,
                             #  generic_ballot 20/7 — see sidecar sample_data)
    house_majority.json      # flagship: 62% vs 55 kalshi, ≥4 model_outputs
    tag_<each-of-8-tags>.json  # one fixture per driver tag (8 files)
    hard_conflict.json       # two credible sources ≥20 pts apart
    thin_data.json           # 1 source, 0 resolved — THIN note expected
  tests/
    test_schemas.py  test_drivers.py  test_conflicts.py  test_report.py
    test_prose_golden.py  test_determinism.py  test_cli.py
    golden/house_majority.report.json   # byte-exact golden
    golden/house_majority.prose.txt
```

## Fixed interfaces (build to these signatures)

```python
# schemas.py
@dataclass(frozen=True) class ModelOutput: model_id: str; source_id: str; p: float; weight: float; inputs_ref: tuple[str, ...]
@dataclass(frozen=True) class MarketSnapshot: venue: str; yes_price: float; liquidity: float | None; captured_at: str
@dataclass(frozen=True) class ExplainRequest: question_id: str; question_text: str; probability: float; as_of: str; model_outputs: tuple[ModelOutput, ...]; market_snapshots: tuple[MarketSnapshot, ...]
def parse_request(raw: dict[str, Any]) -> ExplainRequest   # raises ContractError
class ContractError(ValueError): field_path: str            # "model_outputs[2].p"
# error message format: f"{field_path}: {reason} (got {value!r})"

# credstate.py
@dataclass(frozen=True) class SourceState: source_id: str; alpha: float; beta: float; n_resolved: int
    # credibility property = alpha / (alpha + beta)
def load_fixture_state(path: Path) -> dict[str, SourceState]
def load_db_state(db_path: Path) -> dict[str, SourceState]   # reads terminal sidecar sqlite (sources table); no import of narve_sidecar needed — plain SQL, schema in terminal/sidecar/narve_sidecar/migrations/001_init.sql
def state_for(request: ExplainRequest, state: dict[str, SourceState]) -> dict[str, SourceState]
    # unknown source ⇒ SourceState(alpha=2.0, beta=2.0, n_resolved=0)  (neutral prior)

# drivers.py
TAGS = ("governance","funding","polling","economy","legal","incident","momentum","market_structure")
TAG_OF_MODEL: dict[str, str]   # deterministic mapping, e.g. state_polls→polling,
    # generic_ballot→polling, race_model→momentum, macro_model→economy,
    # poll_aggregator→polling, market_follow→market_structure; unknown model_id
    # → tag by keyword in model_id (poll→polling, fund→funding, court/legal→legal,
    # econ/macro→economy, gov→governance) else "momentum".
def extract_drivers(req: ExplainRequest) -> list[Driver]
    # one driver per contributing model_output, direction "up" iff p > req.probability
    # else "down" (ties count as "up" — deterministic), weight = its normalised
    # weight share × (|p − probability| / Σ|p_i − probability|) blended 50/50 with
    # plain weight share; round 4dp; sort weight desc then source_id asc;
    # label = TAG_LABELS[tag] bare — short human phrase, NO question restatement;
    # evidence_refs = inputs_ref (never empty — parse_request enforces).

# conflicts.py
CONFLICT_THRESHOLD_PTS = 15.0   # between sources with credibility ≥ 0.6
def find_conflicts(req, states) -> list[Conflict]   # pairwise, note format:
    # f"{a} ({a.p:.2f}) and {b} ({b.p:.2f}) are {gap:.0f} pts apart" + both sides' evidence

# would_change.py
def would_change(req, drivers) -> list[str]   # 2–4 items; rank = driver weight ×
    # staleness (as_of − newest evidence ref timestamp when parseable from the
    # request; refs are opaque ids, so staleness falls back to driver weight order);
    # phrasing per tag from a fixed table ("New national generic-ballot print",
    # "Any state-level poll after the next debate", ...). Deterministic.

# prose.py
BANNED = ("it's complicated","it is complicated","many factors","time will tell","hard to say","uncertain times","only time","remains to be seen")
def render_prose(report_parts) -> str   # 3–6 sentences: (1) number vs market,
    # (2) top drivers with direction, (3) main dissent or "no credible dissent",
    # (4) watch-list, (5) confidence. Every sentence traceable to a field;
    # assert no banned phrase (test also greps).
def llm_polish(prose: str, structured: dict) -> str   # seam ONLY: env NARVE_WHY_LLM_KEY;
    # v0 implementation returns prose unchanged with a comment pointing at the seam.

# report.py
def explain(req: ExplainRequest, states: dict[str, SourceState]) -> ExplainReport
def to_json(report) -> str    # sorted keys, 2-space indent, trailing newline —
                              # THE byte-stable serialisation the goldens pin.
# confidence_note rules (exact):
#   THIN — "<n> source[s], <m> resolved call[s] — context is thin" (proper plurals) when
#          len(sources) < 2 or total n_resolved == 0
#   HIGH — "N sources above 0.75 credibility, agreeing within K pts" when
#          ≥3 sources cred>0.75 and max pairwise gap ≤ 5 pts
#   else MODERATE — "N source[s], spread K pts, M resolved call[s]" (proper plurals)

# cli.py
# narve-why explain --in payload.json [--db sqlite] [--state fixtures/sources_state.json]
#   [--format json|text]  (text = the ten-second terminal rendering, monochrome,
#   number + top-2 drivers + dissent + confidence in the first screenful)
# exit 2 on ContractError with the exact field path on stderr.
```

## Rules
- mypy --strict clean; pytest green; no runtime network; stdlib only
  (no pydantic — hand-rolled validation gives the exact-path errors we want).
- Deterministic everywhere: no dict-order reliance, no wall-clock reads inside
  explain() (as_of comes from the request; CLI may not inject now()).
- Fixture numbers MUST match the terminal sample DB (see credstate above) so
  the credibility table cross-checks against the existing engine.
- Process: writes ≤100 lines per Write call; sync bash only; never
  run_in_background; /opt/homebrew/bin/python3.11; no git; only your files.
