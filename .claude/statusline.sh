#!/bin/bash
# Claude Code status line — branch + dashboards up/total + model.
# Reads session JSON from stdin (Claude Code passes context this way),
# falls back to env if jq fails.

set -e

input=$(cat 2>/dev/null || echo "{}")

cwd=$(printf '%s' "$input" | jq -r '.workspace.current_dir // .cwd // empty' 2>/dev/null || true)
[ -z "$cwd" ] && cwd="$PWD"

model=$(printf '%s' "$input" | jq -r '.model.display_name // .model.id // "claude"' 2>/dev/null || echo "claude")

branch=$(git -C "$cwd" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "-")
short_branch="${branch:0:24}"

# Dashboard ports — single lsof scan, then grep
ports="7000 8000 8050 8051 8052 5050 8888 7050 7051 7052 7053 7060"
total=12
listening=$(lsof -iTCP -sTCP:LISTEN -P -n 2>/dev/null | awk 'NR>1 {n=split($9, a, ":"); print a[n]}' | sort -u || true)
up=0
for p in $ports; do
    if printf '%s\n' "$listening" | grep -qx "$p"; then
        up=$((up + 1))
    fi
done

printf '[%s] %d/%d dashboards | %s' "$short_branch" "$up" "$total" "$model"
