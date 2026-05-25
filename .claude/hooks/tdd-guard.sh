#!/usr/bin/env bash
# Lightweight TDD-Guard stand-in.
# If npx tdd-guard is available, defer to it; otherwise no-op silently.
if command -v npx >/dev/null 2>&1 && npx --no-install tdd-guard --version >/dev/null 2>&1; then
  exec npx --no-install tdd-guard "$@"
fi
exit 0
