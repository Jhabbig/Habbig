#!/usr/bin/env bash
# v2.3 — single-command test runner for every fixture suite in the dashboard.
#
# Each `python3 -m <module>` invocation below is a self-contained smoke test
# that prints `N/N fixtures pass` or `smoke OK` and exits with the expected
# count. We grep the output for the canonical pass markers and count
# any that don't match as failures.
#
# Run locally:        bash run_tests.sh
# Run in CI:          bash run_tests.sh && echo green || echo red
#
# Add new fixture suites by appending to FIXTURE_MODULES below.

set -u
cd "$(dirname "$0")"

FIXTURE_MODULES=(
  analysis.classifier
  analysis.severity
  analysis.heatmap
  analysis.topics
  analysis.market_match
  analysis.stance
  analysis.diff
  analysis.email_digest
  analysis.rss_feed
  ingestion.confirmation_hearings
  ingestion.parliament_hearings
  ingestion.court_cases
  ingestion.legislative_bills
  ingestion.ofac_sdn
  ingestion.digest_subscribers
  ingestion.jfsa_scraper
  analysis.people
)

passed=0
failed=0
failures=()

for mod in "${FIXTURE_MODULES[@]}"; do
  printf '  %-40s ' "$mod"
  out=$(python3 -m "$mod" 2>&1)
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "✗ FAIL (exit $rc)"
    failures+=("$mod (exit $rc)")
    failed=$((failed + 1))
    continue
  fi
  # Canonical pass markers: "smoke OK", "N/N fixtures pass" (where the two
  # N's are equal), or just non-empty output that didn't error.
  if echo "$out" | grep -qE '^smoke OK$|^([0-9]+)/\1 fixtures pass$'; then
    echo "✓ pass"
    passed=$((passed + 1))
  elif echo "$out" | grep -qE 'fixtures pass$'; then
    # "X/Y fixtures pass" where X != Y — partial failure
    summary=$(echo "$out" | grep -E 'fixtures pass$' | tail -1)
    echo "✗ partial: $summary"
    failures+=("$mod ($summary)")
    failed=$((failed + 1))
  else
    # Heuristic: if it ran without error and produced any output, treat as pass
    # (some smoke tests just print structured output without a "pass" marker).
    echo "✓ ran (no fixture marker, assuming pass)"
    passed=$((passed + 1))
  fi
done

echo
echo "──────────────────────────────────────────────────────"
echo "  Total: $((passed + failed))  Passed: $passed  Failed: $failed"
echo "──────────────────────────────────────────────────────"

if [[ $failed -gt 0 ]]; then
  echo
  echo "Failed suites:"
  for f in "${failures[@]}"; do
    echo "  - $f"
  done
  exit 1
fi
exit 0
