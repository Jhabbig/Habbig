#!/bin/bash
# UserPromptSubmit hook — inject a one-line summary of which dashboards
# are currently running, so Claude doesn't have to re-discover state.
# Output is JSON consumed by Claude Code; falls silent on any error.

set -e

# Single port scan
listening=$(lsof -iTCP -sTCP:LISTEN -P -n 2>/dev/null | awk 'NR>1 {n=split($9, a, ":"); print a[n]}' | sort -u || true)

up_list=()
for entry in "gateway:7000" "crypto:8000" "stock:8050" "midterm:8051" "traders:8052" "weather:5050" "sports:8888" "world:7050" "voters:7051" "climate:7052" "health:7053" "cb:7060"; do
    name="${entry%%:*}"
    port="${entry##*:}"
    if printf '%s\n' "$listening" | grep -qx "$port"; then
        up_list+=("$name:$port")
    fi
done

if [ ${#up_list[@]} -eq 0 ]; then
    msg="Dashboards running: none (start with ./start_dashboards.sh start or /dash-up <name>)"
else
    msg="Dashboards running (${#up_list[@]}/12): ${up_list[*]}"
fi

# Emit JSON for the hook system. If jq fails, fall back to no injection.
jq -n --arg msg "$msg" '{
  hookSpecificOutput: {
    hookEventName: "UserPromptSubmit",
    additionalContext: $msg
  }
}' 2>/dev/null || echo '{}'
