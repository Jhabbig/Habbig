#!/usr/bin/env bash
# PostToolUse hook: run `ruff check` (F821 only, per ruff.toml) on the
# dashboard directory containing the file Claude just edited.
#
# - Reads the tool input JSON from stdin.
# - Only fires for .py files inside a known top-level service directory.
# - Exits 2 with stderr if ruff finds anything, so Claude sees the report.
# - Silent on success and on files outside the service tree.

set -u

input="$(cat)"
file_path="$(printf '%s' "$input" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("tool_input") or {}).get("file_path") or "")' 2>/dev/null)"

[ -z "$file_path" ] && exit 0
[[ "$file_path" != *.py ]] && exit 0

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -z "$repo_root" ] && exit 0

# File must live under the repo
case "$file_path" in
    "$repo_root"/*) rel="${file_path#$repo_root/}" ;;
    *) exit 0 ;;
esac

# First path segment = top-level dir
top="${rel%%/*}"

# Only lint known Python service dirs (skip workdir/, .snapshots/, etc.)
case "$top" in
    gateway|crypto-dashboard|stock-dashboard|sports-dashboard|world-state-dashboard|\
    climate-dashboard|centralbank-dashboard|top-traders-dashboard|\
    polymarket_weather_dashboard|polymarket_weather_bot|polymarket-bot|\
    Dashboard-x-truth-research-prediction)
        target="$repo_root/$top" ;;
    midterm-dashboard)
        target="$repo_root/midterm-dashboard/backend" ;;
    *) exit 0 ;;
esac

command -v ruff >/dev/null 2>&1 || exit 0

if ! out="$(ruff check "$target" 2>&1)"; then
    {
        echo "ruff check failed in $top after editing $rel:"
        echo "$out"
    } >&2
    exit 2
fi

exit 0
