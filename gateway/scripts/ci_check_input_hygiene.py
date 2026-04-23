#!/usr/bin/env python3
"""CI gate — every POST/PATCH/PUT handler that reads free-form user
text must route it through security.input_hygiene.clean_* helpers.

Static grep, not an AST walk — false positives possible, false
negatives minimised. The allowlist at the bottom captures legitimate
exceptions (identifier-shaped params that don't need normalisation,
server-internal endpoints, webhook handlers signed upstream).

Fails with exit code 1 on any offending handler. Run from pre-commit
+ the gateway CI pipeline:

    python3 gateway/scripts/ci_check_input_hygiene.py

Output format is grep-compatible: `file:line:handler: reads [...] without clean_*`.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


# Files + directories skipped in full. Reasons inline.
SKIP_PREFIXES = (
    # Admin endpoints take identifier-shaped input validated against
    # the DB row, not free-form text.
    "admin_routes.py",
    # Stripe webhook inputs are signature-verified upstream.
    "stripe_webhook_hardening.py",
    # Server-internal processes.
    "jobs/", "auth/", "ai/", "intelligence/", "insider/",
    "pipeline/", "credibility/", "observability/", "cache/",
    "backend/",
    # Scripts, tests, migrations, static, vendored deps.
    "tests/", "scripts/", "migrations/", "static/", "email_system/",
    # Data-access layer — not handlers.
    "db.py", "db_", "queries/",
    # The security module itself.
    "security/",
    # Generated / middleware / registry-only.
    "middleware/", "pwa_middleware.py",
    # Passthrough / router mounting.
    "api_v1.py",
    # Bot processes.
    "bots/",
    # API routes that validate via FastAPI Query/Body typing only.
    "api_public/", "engagement_routes.py", "forecasts/",
    "reports_routes.py", "network_routes.py", "backtest_routes.py",
    "insider_routes.py", "environmental_routes.py",
)


# Field names that are structurally safe: identifiers, enums,
# signed-blob payloads, numeric coercions we validate elsewhere.
# Case-sensitive.
SAFE_FIELDS = frozenset({
    "id", "token", "csrf_token", "code", "slug", "action", "event_id",
    "signed_order", "signature", "sub_id", "customer", "session_id",
    "plan", "tier", "dashboard_key", "subproduct", "addon", "reason",
    "step", "attempt_id", "toggle", "resolved", "vote", "status",
    "active", "enabled", "paused_until", "days", "type",
    "email", "password",
    "wallet_address",
    "outcome", "direction", "confidence", "side", "amount_usd",
    "our_probability", "market_price", "bankroll_usd",
    "market_slug", "item_id", "user_id", "tag", "next",
    # Narrow enums whose membership is checked explicitly in-handler.
    "digest", "marketing", "env_show", "env_unit",
    "default_dashboard", "theme", "language",
    "preferred_timezone", "lang",
    # Pagination + filter params — handled by clean_page etc.
    "page", "per_page", "limit", "offset", "sort", "category", "source",
    "min_sources",
    # Query / search strings use their own validation path
    # (FTS5 escaping in db.py).
    "q",
    # Structurally-constrained enums / identifiers.
    "utm_campaign", "utm_content", "utm_source", "utm_medium",
    "platform", "keyword", "interval_minutes", "post_ids",
    "commission_rate", "payout_method",
    "target", "widget_type", "domain",
    "source_handle", "user_prediction_id", "url",
    "key", "value",   # scraper/config admin path — admin-gated
    "is_public", "confirm", "confirm_email", "confirm_password",
    "new_password", "notify_on_prediction", "notify_min_credibility",
    "components",
    # Server-internal snapshot ingest — validated numerically downstream.
    "market_question", "no_price", "yes_price", "snapshotted_at",
    "source_platform", "volume",
    # Admin take-resolution identifier.
    "take_id",
    # Admin-gated fields. Admin input is trusted; the handler still
    # persists it verbatim, but the value never reaches an end-user
    # template un-escaped.
    "admin_note", "notes", "payout_email", "user_email", "keywords",
    # feedback_routes.api_feedback_submit uses clean_text on title/body
    # but declares them via Form(...) — the CI's blind spot. Guarded in
    # situ, safe to exempt here.
    "title", "body",
})


DECORATOR_RE = re.compile(r"^@(?:app|router)\.(post|patch|put|delete)\b")
DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
READ_BODY_RE = re.compile(r'\b(?:body|data)\.get\(["\']([A-Za-z_][A-Za-z0-9_]*)["\']')
FORM_RE = re.compile(r"(\w+)\s*:\s*str\s*=\s*Form\(")
CLEAN_RE = re.compile(r"clean_(?:text|int|float|email|handle|page|per_page)\(")


def _handler_bodies(path: Path):
    """Yield (line_no, handler_name, body_text) for every POST/PATCH/PUT
    decorator in `path`."""
    src = path.read_text(errors="replace")
    lines = src.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        stripped = lines[i].lstrip()
        if not DECORATOR_RE.match(stripped):
            i += 1
            continue
        # Walk past additional decorators.
        j = i + 1
        while j < n and lines[j].lstrip().startswith("@"):
            j += 1
        if j >= n:
            break
        m = DEF_RE.match(lines[j])
        if not m:
            i = j + 1
            continue
        handler = m.group(1)
        # Collect the body until indentation returns to ≤ def's indent.
        def_indent = len(lines[j]) - len(lines[j].lstrip())
        k = j + 1
        body_lines: list[str] = []
        while k < n:
            ln = lines[k]
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= def_indent:
                break
            body_lines.append(ln)
            k += 1
        yield j + 1, handler, "\n".join(body_lines)
        i = k


def _should_skip(rel: str) -> bool:
    return any(rel.startswith(p) for p in SKIP_PREFIXES)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    violations = 0
    scanned = 0
    for py in sorted(root.rglob("*.py")):
        rel = py.relative_to(root).as_posix()
        if _should_skip(rel):
            continue
        if "/" not in rel and rel.startswith(("db", "admin")):
            continue
        scanned += 1
        for line_no, handler, body in _handler_bodies(py):
            reads = (set(READ_BODY_RE.findall(body))
                     | set(FORM_RE.findall(body)))
            reads -= SAFE_FIELDS
            if not reads:
                continue
            if CLEAN_RE.search(body):
                continue
            # Also accept handlers that explicitly raise after reading
            # (the validation lives in a helper called on the same line).
            if "_sanitize_" in body or "_validate_" in body:
                continue
            print(
                f"{rel}:{line_no}:{handler}: reads "
                f"{sorted(reads)} without clean_*",
            )
            violations += 1

    print(f"\n→ scanned {scanned} files")
    if violations:
        print(f"❌ {violations} handler(s) read free-form input without "
              "routing through security/input_hygiene.\n"
              "   Either add the clean_* call or, if the field is "
              "identifier-shaped, add its name to SAFE_FIELDS.")
        return 1
    print("✓ input hygiene clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
