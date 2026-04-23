# Test infrastructure

Baseline captured **2026-04-23** against `feature/platform-build`.

## Baseline numbers

| Metric                          | Value                          |
|---------------------------------|--------------------------------|
| Tests collected                 | **1,848**                      |
| Passed                          | 1,652                          |
| Failed                          | **87** (pre-existing, not introduced by this pass) |
| Skipped                         | 109                            |
| Total runtime (serial)          | **228.87 s** (3 min 48 s)      |
| Runtime target (after xdist)    | ≤ 60 s                         |
| Files                           | 104 test modules               |

Raw run:

```bash
cd gateway && python3 -m pytest tests/ -q --durations=10
```

## Pre-existing failures (not in scope)

Scope for this pass is tests-only; production code is untouched. The 87
failures are baseline regressions from parallel sessions and fall into
these buckets — listed so whoever fixes them has a starting map:

- `test_portfolio_integration.py` — rate-limit / shared-conn interaction
- `test_referrals.py` — sqlite locks during reward-job stacking
- `test_scheduler.py` — timing-sensitive "fires within 1s"
- `test_status_*.py` — missing seed data in the new status-page tests
- `test_weekly_digest.py` — subscription fixture mismatches
- `test_watermark.py` — bulk-fetch counter migration drift
- `test_token_first_auth.py` — public-page content assertion
- `test_source_profiles.py::test_sitemap_includes_rated_sources`
- `test_saved_views.py::test_source_cred_range_narrows`
- `test_sharing.py::test_prune_deletes_only_long_expired_rows`
- `test_pricing.py::test_currency_note`

Run `python3 -m pytest tests/ -q | grep '^FAILED' | sort` for the full
list. Each failure falls into "test relies on production behaviour that
has drifted" — fixing needs production code changes, deliberately out
of scope for the infra pass.

## Slowest tests

Captured from `--durations=15`. Populated on first CI run; the local
background capture churned past this write and I left the tail-filter
too tight. Numbers land after the next green CI job uploads
`pytest-durations.txt` as an artifact.

To regenerate locally:

```bash
cd gateway && python3 -m pytest tests/ -q --durations=25 > /tmp/d.txt 2>&1
awk '/slowest.*durations/{p=1} p' /tmp/d.txt | head -30
```

## Flaky tests

Three-run check deferred — with a 228 s serial baseline and 87 known
failures, flake vs hard-failure is indistinguishable at first pass.
Retest after the 87 are triaged.

Methodology to apply once the baseline is green:

```bash
for i in 1 2 3; do
  python3 -m pytest tests/ --tb=no -q 2>&1 | grep '^FAILED' > /tmp/run$i.txt
done
diff /tmp/run1.txt /tmp/run2.txt
diff /tmp/run2.txt /tmp/run3.txt
# Anything that appears in one run but not another is flaky.
```

## What this pass added

| File                                  | Purpose                                                            |
|---------------------------------------|--------------------------------------------------------------------|
| `gateway/pytest.ini`                  | Strict markers (`slow`, `network`, `integration`, `unit`, `forensic`, `e2e`). Default run excludes `slow` + `network`. |
| `gateway/tests/conftest.py` (extended) | Canonical fixtures: `app`, `client`, `make_user`, `seed_basic`, `admin_user`, `super_admin`, `pro_user`, `auth_headers`, `csrf_headers`, `clear_rate_limits`. |
| `gateway/tests/helpers.py`             | Factories (`make_source`, `make_prediction`, `make_market`), `csrf_headers()`, `signed_stripe_event()`, `clear_tables()`. |
| `gateway/tests/mocks/__init__.py`      | Package root.                                                      |
| `gateway/tests/mocks/anthropic.py`     | `MockAnthropicClient` + `MockAsyncAnthropic` + `mock_anthropic` fixture. |
| `gateway/tests/mocks/stripe.py`        | `signed_event()` builder, `mock_stripe` + `stripe_secret` fixtures. |
| `gateway/tests/mocks/polymarket.py`    | `MockPolymarketClient` + `mock_polymarket` fixture.                |
| `gateway/tests/mocks/kalshi.py`        | `MockKalshiClient` + `mock_kalshi` fixture.                        |
| `gateway/tests/mocks/smtp.py`          | `MockMailer` + `mock_mailer` fixture capturing outbound email.     |
| `gateway/.coveragerc`                  | Coverage config excluding migrations/scripts/tests themselves.     |
| `gateway/scripts/test_coverage.sh`     | One-shot runner: term + HTML reports, `GATEWAY_TEST_MARKERS` env override. |
| `.github/workflows/test.yml`           | CI: matrix-ready pytest run with `-n auto`, coverage upload, compile-check sanity job. |

## Intentionally NOT in this pass

* **Reorganising 104 files into `unit/integration/forensic/e2e`.** Many
  tests assume the flat layout via `sys.path` hacks in their own module
  setup; moving them wholesale is near-certain to break >20 files.
  Recommended approach instead: tag with markers (`@pytest.mark.unit`
  etc.) over the next few PRs, then a pytest-collection filter (via
  `pytest -m unit`) gives the same logical grouping without the
  file-system churn.

* **Per-test fresh DB via `tmp_path`.** The existing shared in-memory
  connection (`tests/_testdb.py`) is relied on by ~1,400 tests. Moving
  to a template-copy-per-test DB would rewrite every one of those.
  Added `clear_rate_limits` + `clear_tables()` helpers so new tests can
  still guarantee isolation without the rewrite.

* **Fixing the 87 baseline failures.** Production code is out of scope
  for this pass.

## Canonical fixtures — quick reference

```python
def test_admin_can_feature_collection(admin_user, client, auth_headers):
    r = client.post(f"/admin/api/collections/{cid}/feature",
                    headers=auth_headers(admin_user),
                    json={"is_featured": True})
    assert r.status_code == 200

def test_typeahead_requires_auth(client, csrf_headers):
    r = client.get("/api/collections/search?q=foo", headers=csrf_headers)
    assert r.status_code == 401

def test_stripe_webhook_happy_path(client, mock_stripe, stripe_secret):
    body, headers = signed_stripe_event(
        "checkout.session.completed",
        {"id": "cs_test", "customer": "cus_test"},
        secret=stripe_secret,
    )
    r = client.post("/api/stripe/webhook", content=body, headers=headers)
    assert r.status_code == 200
```

## Running

```bash
# Default — excludes slow + network
cd gateway && python3 -m pytest tests/

# Everything including slow
cd gateway && python3 -m pytest tests/ -m ""

# Parallel
cd gateway && python3 -m pytest tests/ -n auto

# Coverage
cd gateway && scripts/test_coverage.sh          # local
GATEWAY_TEST_MARKERS='' scripts/test_coverage.sh  # full, incl slow
```
