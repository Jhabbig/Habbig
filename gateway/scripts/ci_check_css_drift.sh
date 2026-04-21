#!/usr/bin/env bash
# CI gate for the narve.ai design system.
#
# Run by pre-commit + the design-system CI workflow. Fails the build
# if any of these drift vectors show up in gateway/static/:
#
#   1. Hardcoded hex outside tokens.css — all colour must flow through
#      a CSS variable. @font-face / data URIs / comment text / var()
#      fallback syntax are all legitimate exceptions.
#
#   2. var(--X) referencing a token that tokens.css doesn't define. A
#      typo or a stale rename is the usual cause; either bug would
#      silently render "initial" in production.
#
#   3. Raw z-index > 50 outside tokens.css — the --z-* scale covers
#      every layer we use and consolidates the stacking context.
#
# Exit code 0 = clean, 1 = drift. Output is grep-style so editors can
# jump straight to offenders.

set -euo pipefail

cd "$(dirname "$0")/.."
STATIC_DIR="static"
TOKENS_FILE="tokens.css"

fail() {
  echo "❌ $1"
  exit 1
}

cd "$STATIC_DIR"

# Strip CSS comments (/* ... */, possibly multi-line) before scanning
# — otherwise a legitimate "AA 4.54:1 on #fff" commentary triggers the
# hex check. Uses perl for sanity: sed's multi-line handling varies
# between BSD + GNU.
strip_comments() {
  # shellcheck disable=SC2016
  perl -0777 -pe 's{/\*.*?\*/}{}gs' "$1"
}

# ── 1. No hardcoded hex outside tokens ───────────────────────────────
HEX_VIOLATIONS=""
for f in *.css; do
  [ "$f" = "$TOKENS_FILE" ] && continue
  stripped=$(strip_comments "$f")
  # Keep line numbers by grepping line-by-line — strip_comments
  # preserves newlines, so line N in the stripped text == line N in
  # the original file.
  offending=$(printf '%s\n' "$stripped" | grep -nE "#[0-9a-fA-F]{3,8}\b" \
    | grep -vE "url\(data:" \
    | grep -vE "@font-face" \
    | grep -vE "var\(--[^,)]+,\s*#" \
    || true)
  if [ -n "$offending" ]; then
    while IFS= read -r line; do
      HEX_VIOLATIONS="${HEX_VIOLATIONS}${f}:${line}
"
    done <<< "$offending"
  fi
done
if [ -n "$HEX_VIOLATIONS" ]; then
  echo "Hardcoded hex colours outside tokens.css:"
  echo "$HEX_VIOLATIONS"
  fail "Move these to tokens.css or replace with var(--existing-token)."
fi

# ── 2. No undefined variables ────────────────────────────────────────
# (The usage side is taken from every *.css, not just tokens; the
#  definition side is ONLY tokens.css so a var defined inside a
#  component file doesn't count — those should live in tokens.)
tmp_used=$(mktemp)
tmp_def=$(mktemp)
trap 'rm -f "$tmp_used" "$tmp_def"' EXIT

# Collect usage from every non-tokens CSS file. tokens.css is
# intentionally excluded — it defines tokens, it doesn't "use" them
# outside of back-compat aliases that already cross-reference, and
# scanning it catches placeholder var(--name) appearances in doc
# comments.
for f in *.css; do
  [ "$f" = "$TOKENS_FILE" ] && continue
  strip_comments "$f" | grep -oE "var\(--[a-zA-Z0-9_-]+" || true
done | sed 's/var(//' | sort -u > "$tmp_used"
# Definition side: match "^  --name:" at the START of a line. Avoids
# picking up var(--x) references inside alias lines like
# `--red: var(--rank-3);`. Works on BSD + GNU grep.
grep -hE "^\s+--[a-zA-Z0-9_-]+\s*:" "$TOKENS_FILE" \
  | sed -E 's/^[[:space:]]+(--[a-zA-Z0-9_-]+).*/\1/' | sort -u > "$tmp_def"
# Legitimate exceptions: mobile-a11y.css + scroll-animations.css + the
# component-specific CSS files define a handful of locally-scoped vars
# that shouldn't live in tokens.css (eg. --dash-transition-fast tied
# to a specific animation). Strip those before comparing.
LOCAL_VARS=$(grep -hE "^\s+--[a-zA-Z0-9_-]+\s*:" \
  mobile-a11y.css scroll-animations.css skeletons.css states.css gateway.css 2>/dev/null \
  | sed -E 's/^[[:space:]]+(--[a-zA-Z0-9_-]+).*/\1/' | sort -u)
UNDEFINED=$(comm -23 "$tmp_used" "$tmp_def" \
  | (if [ -n "$LOCAL_VARS" ]; then grep -Fxvf <(echo "$LOCAL_VARS"); else cat; fi) \
  || true)
if [ -n "$UNDEFINED" ]; then
  echo "Variables used but not defined in ${TOKENS_FILE}:"
  echo "$UNDEFINED"
  fail "Add the token to ${TOKENS_FILE} or fix the typo in the call site."
fi

# ── 3. No raw z-index > 50 outside tokens ────────────────────────────
# Skip tokens.css because that's where the --z-* scale is DEFINED.
# Skip rules that reference a variable (var(--z-dropdown) is fine).
RAW_Z=$(grep -rnE "^\s+z-index:\s*[0-9]+" *.css 2>/dev/null \
  | grep -v "^${TOKENS_FILE}:" \
  | awk -F: '
      {
        val = $NF + 0
        if (val > 50) print $0
      }
    ' \
  || true)
if [ -n "$RAW_Z" ]; then
  echo "Raw z-index values > 50 outside tokens.css:"
  echo "$RAW_Z"
  fail "Use var(--z-dropdown/--z-sticky/--z-modal/--z-toast/--z-overlay) instead."
fi

echo "✓ design system clean"
