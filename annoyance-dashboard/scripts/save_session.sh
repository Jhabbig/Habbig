#!/usr/bin/env bash
# Commit the current session's work to a local save/* branch + a patch
# file backup. Never pushes. Run push_saved.sh later to ship.
#
# Usage:
#   ./scripts/save_session.sh P1
#   ./scripts/save_session.sh P2
set -euo pipefail

SESSION_TAG="${1:?usage: save_session.sh <P0|P1|P2|P3|P4|P5|P6>}"
REPO_ROOT="/Users/shocakarel/Habbig/annoyance-dashboard"
TS=$(date -u +%Y%m%dT%H%M%SZ)
BRANCH="save/${SESSION_TAG}-${TS}"

cd "$REPO_ROOT"

# Bail early if a merge/rebase/cherry-pick/bisect is already in progress —
# creating a save branch on top of half-finished state is a footgun.
GIT_DIR=$(git rev-parse --git-dir)
for f in MERGE_HEAD REBASE_HEAD CHERRY_PICK_HEAD BISECT_LOG; do
  if [ -e "$GIT_DIR/$f" ]; then
    echo "REFUSING to save — $GIT_DIR/$f exists (merge/rebase/cherry-pick/bisect in progress)"
    echo "  resolve it first: git status"
    exit 2
  fi
done

# Scope: this repo tracks multiple dashboards under one toplevel — stage only
# files under annoyance-dashboard/ so we don't pull sibling-session work into
# a P-tagged save. If the caller has anything under this dir to commit, they
# still get the full save flow.
if [ -z "$(git status --porcelain -- .)" ]; then
  echo "nothing to save from $SESSION_TAG under $REPO_ROOT"
  exit 0
fi

# Safety: no secrets, no databases (scoped to this directory).
# Covers SQLite main + all three possible sidecars (WAL, journal, shm) plus
# the gateway's auth.db in case the session accidentally touched it.
for risky in .env annoyance.db annoyance.db-journal annoyance.db-shm annoyance.db-wal auth.db; do
  if git status --short -- . | grep -q "$risky"; then
    echo "REFUSING to stage $risky — add to .gitignore or untrack first"
    exit 2
  fi
done

# Py-compile gate — fail early on syntax errors (only check files we'll stage)
STAGED_PY=$(git status --short -- . | awk '{print $2}' | grep -E '\.py$' || true)
if [ -n "$STAGED_PY" ]; then
  echo "$STAGED_PY" | xargs python3 -m py_compile
fi

# Snapshot current HEAD so we can diff after the branch move
BASE=$(git rev-parse HEAD)

# Create the save branch from current HEAD
git checkout -b "$BRANCH"

SUMMARY=$(git status --short -- . | head -8 | awk '{print $2}' | xargs -I{} basename {} | paste -sd, -)

# Stage only files under annoyance-dashboard/ — sibling-session work in
# ../gateway/ or other dashboards stays out of this save.
git add -A -- .
git commit -m "$(cat <<EOF
[$SESSION_TAG save $TS] $SUMMARY

$(git diff --cached --stat | tail -1)

Saved, not pushed. To ship later:
  git checkout feature/annoyance-polish
  git merge --no-ff $BRANCH
  git push origin feature/annoyance-polish

Or apply the patch directly:
  git am saves/${SESSION_TAG}-${TS}.patch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"

# Write patch file too — survives even if branch gets deleted
mkdir -p saves
git format-patch "$BASE"..HEAD --stdout > "saves/${SESSION_TAG}-${TS}.patch"

# Log to coordination
printf "[%s] [%s] SAVED on %s (patch: saves/%s-%s.patch)\n" \
  "$TS" "$SESSION_TAG" "$BRANCH" "$SESSION_TAG" "$TS" >> COORDINATION.md

echo "✅ $SESSION_TAG saved"
echo "   branch: $BRANCH"
echo "   patch:  saves/${SESSION_TAG}-${TS}.patch"
echo "   commit: $(git rev-parse --short HEAD)"
echo ""
echo "To push later, run: scripts/push_saved.sh"
