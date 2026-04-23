#!/usr/bin/env bash
# CI gate: every POST handler that reads free-form body text MUST run
# the input through security.input_hygiene, NOT through hand-rolled
# `.strip()[:N]` slicing.
#
# Without this check, agents re-introduce the "we accept null bytes"
# class of bug every time they ship a new feature (see the 2026-04-23
# edge-case sweep for the list of handlers that had to be retroactively
# fixed).
#
# The check is a static grep — false positives are possible, so we
# maintain an allowlist of intentional callsites that don't need the
# clean_* helpers (identifier-shaped params, enum-shaped params that
# are validated via an explicit membership check).
#
# Fails the build on any POST handler that:
#   a) exists outside the allowlist, AND
#   b) reads `body.get("foo")` or `request.form()["foo"]` into a name,
#      AND
#   c) has no `clean_text(` / `clean_int(` / `clean_float(` /
#      `clean_email(` / `clean_handle(` in the same function body.
#
# Run from pre-commit + the gateway CI pipeline:
#   bash gateway/scripts/ci_check_input_hygiene.sh

set -euo pipefail

cd "$(dirname "$0")/.."

# Files excluded from the check. Reasons listed inline.
ALLOW_FILES=(
  # Admin endpoints — take identifier-shaped input (ids, enum strings)
  # that's validated against the DB row, not free-form text. Adding
  # clean_text here would just add noise.
  "admin_routes.py"
  # Stripe webhook — every field is validated by signature.
  "stripe_webhook_hardening.py"
  # Job registry & jobs/* — server-internal, never touches user text.
  "jobs/"
  # Auth flows — passwords + tokens use a different validation layer
  # (rate_limiter + hashlib) and shouldn't be re-normalised.
  "auth/"
  # AI client wrapper — server-side only; prompts are ours.
  "ai/"
  "intelligence/"
)

# Handlers inside the following files that DO need hygiene coverage.
# Populated by walking ``*.py`` in gateway/ and stripping the allowlist.
SCAN_FILES=$(python3 - <<'PY'
import os
import re

root = os.getcwd()
allow_prefixes = [
    "admin_routes.py", "stripe_webhook_hardening.py",
    "jobs/", "auth/", "ai/", "intelligence/",
    # Tests + scripts are out of scope.
    "tests/", "scripts/",
    # db.py + db_*.py are data-access layer, not handlers.
    "db.py", "db_",
]
scan = []
for dirpath, _, filenames in os.walk(root):
    rel = os.path.relpath(dirpath, root)
    if rel.startswith((".", "static", "__pycache__", "migrations")):
        continue
    for f in filenames:
        if not f.endswith(".py"):
            continue
        p = os.path.join(rel, f) if rel != "." else f
        if any(p.startswith(a) for a in allow_prefixes):
            continue
        scan.append(p)
print("\n".join(sorted(scan)))
PY
)

violations=0
echo "→ scanning $(echo "$SCAN_FILES" | wc -l | tr -d ' ') handler files"

while IFS= read -r file; do
  [ -f "$file" ] || continue

  # Extract POST/PATCH/PUT handler bodies. Python's indentation means
  # we can use awk with a "collect until dedent" state machine.
  python3 - "$file" <<'PY' || violations=$((violations + $?))
import re, sys
path = sys.argv[1]
src = open(path).read()
lines = src.splitlines()

# Find decorators for mutating HTTP methods.
decorator_re = re.compile(r'^@(?:app|router)\.(post|patch|put|delete)\b')
# Read-body patterns that indicate free-form user input.
read_body_re = re.compile(r'\b(?:body|data)\.get\(["\']([A-Za-z_][A-Za-z0-9_]*)["\']')
form_re = re.compile(r'(\w+)\s*:\s*str\s*=\s*Form\(')
# Sanitised-call patterns.
clean_re = re.compile(r'clean_(?:text|int|float|email|handle|page|per_page)\(')
# Fields that are fine without clean_* — identifiers, booleans, numeric
# coercions we handle elsewhere, CSRF / webhook tokens, signed order
# blobs, signed payloads. Names are case-sensitive.
SAFE_FIELDS = {
    "id", "token", "csrf_token", "code", "slug", "action", "event_id",
    "signed_order", "signature", "sub_id", "customer", "session_id",
    "plan", "tier", "dashboard_key", "subproduct", "addon", "reason",
    "step", "attempt_id", "toggle", "resolved", "vote", "status",
    "active", "enabled", "paused_until", "days",
    "email", "password",   # each has its own dedicated clean_email /
                           # password-hash path
    "wallet_address",      # validated by regex in portfolio/polymarket
    "outcome", "direction",
}

i = 0
n = len(lines)
file_violations = 0
while i < n:
    line = lines[i]
    m = decorator_re.match(line.lstrip())
    if not m:
        i += 1
        continue
    # Locate the `async def` / `def` that follows — may be 1-3 lines down
    # past multi-line decorators.
    j = i + 1
    while j < n and lines[j].strip().startswith("@"):
        j += 1
    if j >= n:
        break
    def_match = re.match(r'\s*(?:async\s+)?def\s+(\w+)\s*\(', lines[j])
    if not def_match:
        i = j + 1
        continue
    handler = def_match.group(1)
    # Walk until the function dedents back to top level.
    body_start = j + 1
    k = body_start
    indent = None
    while k < n:
        ln = lines[k]
        if not ln.strip():
            k += 1; continue
        lead = len(ln) - len(ln.lstrip())
        if indent is None:
            indent = lead
        if lead < indent and ln.strip():
            break
        k += 1
    body = "\n".join(lines[body_start:k])
    # Skip handlers that never touch body/form.
    reads = set(read_body_re.findall(body)) | set(form_re.findall(body))
    reads -= SAFE_FIELDS
    if not reads:
        i = k; continue
    if clean_re.search(body):
        i = k; continue
    print(f"{path}:{j+1}:{handler}: reads {sorted(reads)} without clean_* call")
    file_violations += 1
    i = k

sys.exit(1 if file_violations else 0)
PY
done <<< "$SCAN_FILES"

if [ "$violations" -gt 0 ]; then
  echo ""
  echo "❌ $violations handler(s) read free-form input without routing through"
  echo "   security/input_hygiene. Either add the clean_* call or, if the"
  echo "   field is identifier-shaped, add its name to SAFE_FIELDS above."
  exit 1
fi
echo "✓ input hygiene clean"
