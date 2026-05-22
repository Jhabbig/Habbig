#!/usr/bin/env bash
# PreToolUse hook: gate edits to the gateway auth surface behind an explicit
# one-shot ack. Without the ack file, the edit is blocked and Claude is told
# to confirm with the user first.
#
# Ack file: $CLAUDE_PROJECT_DIR/.claude/.auth-edit-ack
#   - Must exist
#   - Must be < 300 seconds old
#   - Consumed (deleted) on use, so each sensitive edit needs its own ack
#
# Sensitive paths (relative to repo root):
#   gateway/db.py
#   gateway/server.py
#   gateway/.env*
#   any file matching auth.db (shouldn't ever be edited, belt + braces)

set -u

input="$(cat)"
file_path="$(printf '%s' "$input" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("tool_input") or {}).get("file_path") or "")' 2>/dev/null)"

[ -z "$file_path" ] && exit 0

repo_root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -z "$repo_root" ] && exit 0

case "$file_path" in
    "$repo_root"/*) rel="${file_path#$repo_root/}" ;;
    *) exit 0 ;;
esac

sensitive=0
case "$rel" in
    gateway/db.py|gateway/server.py) sensitive=1 ;;
    gateway/.env|gateway/.env.*)     sensitive=1 ;;
    *auth.db*)                       sensitive=1 ;;
esac

[ "$sensitive" -eq 0 ] && exit 0

ack="$repo_root/.claude/.auth-edit-ack"
if [ -f "$ack" ]; then
    # Stale acks (>5 min) don't count
    if [ "$(( $(date +%s) - $(stat -c %Y "$ack" 2>/dev/null || stat -f %m "$ack" 2>/dev/null || echo 0) ))" -lt 300 ]; then
        rm -f "$ack"
        exit 0
    fi
    rm -f "$ack"
fi

cat >&2 <<EOF
BLOCKED: $rel is part of the gateway auth surface.

Per gateway/CLAUDE.md, edits here need explicit user confirmation.

To proceed:
  1. Use AskUserQuestion to confirm the user wants this specific change
     (describe the change, including the rough diff).
  2. If they approve, run via Bash:
        touch "\$CLAUDE_PROJECT_DIR/.claude/.auth-edit-ack"
  3. Retry the same edit. The ack is one-shot and expires after 5 minutes.

If the user said no, do not retry.
EOF
exit 2
