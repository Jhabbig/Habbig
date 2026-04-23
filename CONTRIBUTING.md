# Contributing

This repo is built by a mix of humans and Claude sessions running in
parallel on the same branch. The rules below exist because parallelism
bites: two sessions picking migration 124 will collide; two sessions
rewriting `server.py` will end up merging each other's ghost edits. Read
before committing.

---

## Development environment

- Python 3.11+ (prod runs 3.12)
- SQLite 3.35+ (for FTS5 + JSON1 + `ALTER TABLE DROP COLUMN`)
- Node 20 (only needed for `extension/`)
- `gateway/requirements.txt` is pinned — **do not** bump casually; the
  last audit date is in the comment header.

Install and run:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r gateway/requirements.txt
cp gateway/.env.example gateway/.env
cd gateway && python3 -m uvicorn server:app --reload --port 7000
```

The first run prints a one-time dev invite token to stdout if
`invite_tokens` is empty — use it at `/token`.

---

## Parallel session discipline

Multiple Claude sessions commit to `feature/platform-build` concurrently.
Treat every session as untrusted sharing a live branch.

**Before every session:**

```bash
cd ~/Habbig
git pull
git log --oneline -15   # see what sibling agents landed
ls gateway/migrations/*.py | tail -5   # current head
```

**Before every commit:**

```bash
git pull --rebase
cd gateway && python3 -m pytest tests/ -q   # your slice must pass
```

**After every deploy:**

```bash
ssh julianhabbig@100.69.44.108 \
    "cd ~/Habbig/gateway && git add -A && git commit -m 'deploy: <summary>'"
# Server tracks its own history; skip the commit and your next restart
# will get reset by a stray git op.
```

**Push policy:** by default pushes to `origin` are gated on explicit
operator approval. Individual sessions may be told to commit locally
only ("dont push and deploy"). If you're an agent session and you're
unsure, ask.

---

## Migration rules

Migrations live at `gateway/migrations/NNN_slug.py`. The current head is
`130_feedback.py`.

- Each file exposes `revision: str` and `down_revision: str` (both
  zero-padded three-digit) plus `upgrade(c)` and `downgrade(c)`.
- **Filename may not match revision.** E.g. `022_notifications.py` has
  `revision = "026"` historically. Trust `revision`, not the filename.
- **Pick your migration range per session.** Coordinate via the
  operator ("your range is 131-133"); collisions trigger
  `schema_version` conflicts at startup.
- Guard every DDL with `IF NOT EXISTS` / column-presence checks — the
  migration has to be replayable against a partial DB.
- Indexes: always `CREATE INDEX IF NOT EXISTS`.
- Foreign keys: `ON DELETE CASCADE` for data that's meaningless without
  its owner (votes, reports). `ON DELETE SET NULL` for things we want
  to retain for audit (take.user_id when a user deletes their account).
- Never `DROP TABLE` on a table another session might be mid-write on —
  if you really need to drop, announce it and schedule it.

---

## Code quality rules

- `sqlite3.Row` objects have no `.get()`. Always use `row["key"]`; a
  `KeyError` is OK, it tells you your schema assumption is wrong.
- `render_page(name, context)` substitutes `{{ key }}`. For HTML-carrying
  values, use `raw_key=...` — the renderer skips escaping for any key
  starting with `raw_`.
- **Do not** expand `server.py` or `db.py` with new features. New
  routes go in a `*_routes.py` module that registers itself via
  `register(app)` or top-level `@app.get` / `@app.post`. New DB helpers
  go in a `db_<feature>.py` module.
- Inline styles in HTML: acceptable for one-off overrides, unacceptable
  when the same pattern repeats. Three repeats → extract a class into
  `gateway/static/gateway.css`.
- Never commit secrets. `.env` is gitignored. Tokens, API keys, and
  session secrets live in the server's `/etc/gateway.env` or the
  operator's `~/.gateway_env`.
- Monochrome only. Do not introduce new hues. The two exceptions are
  correctness badges (green tick / red cross on resolved-correct takes
  and predictions).
- Inter font, self-hosted subset. Do not pull Google Fonts.

---

## Testing

```bash
cd gateway
python3 -m pytest tests/ -q                    # full suite
python3 -m pytest tests/test_market_takes.py   # one file
python3 -m pytest tests/ -q --cov=. --cov-report=term-missing
python3 -m pytest tests/ -q --cov=. --cov-fail-under=60
```

- 100+ test files live under `gateway/tests/`.
- Tests use `tests/_testdb.py` for a shared in-memory SQLite with all
  migrations applied.
- Feature-gated tests (`pytest.mark.skipif(not <feature_available>)`)
  auto-resume when the underlying code lands.
- Historical cross-file DB contamination is documented in
  [TEST_COVERAGE.md](TEST_COVERAGE.md); running a single file in
  isolation is the reliable path when adding new tests.

---

## Security

- Every POST/PUT/PATCH/DELETE needs CSRF unless explicitly exempt in
  `_CSRF_EXEMPT_POSTS` / `_CSRF_EXEMPT_POST_PREFIXES` in `server.py`.
  Webhooks (Stripe) and pre-session POSTs (newsletter signup) are the
  common exemptions.
- Rate-limit every unauthenticated POST. Default bucket is per-IP
  (`_is_rate_limited(key, limit, window)`), plus per-email on anything
  that touches a user address.
- Never log sensitive payloads. Mask emails via `db.mask_email(...)` in
  security-event logs.
- Paid content is gated server-side in `_is_paid()` (see
  `take_routes.py`, `user_prediction_routes.py`). Admin routes go
  through `_require_admin_user`.
- Every user-visible API list response is passed through
  `forensics.signer.sign_response(user_id, data, endpoint)` so leaks
  are attributable. Don't bypass this.

Full posture: [gateway/NARVE_SECURITY_AUDIT.md](gateway/NARVE_SECURITY_AUDIT.md).
Report via [SECURITY.md](SECURITY.md).

---

## Docs

Root-level docs are truth-of-record:

- [README.md](README.md) — public-facing project overview
- [RUNBOOK.md](RUNBOOK.md) — deploy + incident response
- [ARCHITECTURE.md](ARCHITECTURE.md) — system design
- [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) — tokens, components, usage rules
- [CHANGELOG.md](CHANGELOG.md) — user-visible changes per release
- [SECURITY.md](SECURITY.md) — disclosure policy
- [API.md](API.md) — public API reference

Doc reviews are part of every session. If you change behaviour, update
the doc in the same commit.
