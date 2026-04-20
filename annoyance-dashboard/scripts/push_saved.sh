#!/usr/bin/env bash
# Merge all save/* branches into feature/annoyance-polish and push.
# Run once, when you're ready to ship the whole stack of save_session.sh runs.

set -euo pipefail

REPO_ROOT="/Users/shocakarel/Habbig/annoyance-dashboard"
TARGET="feature/annoyance-polish"
BASE="feature/platform-build"

cd "$REPO_ROOT"

# Bail early if a merge/rebase/cherry-pick is already in progress — we'd
# clobber the in-flight state if we tried to chain another merge on top.
GIT_DIR=$(git rev-parse --git-dir)
for f in MERGE_HEAD REBASE_HEAD CHERRY_PICK_HEAD; do
  if [ -e "$GIT_DIR/$f" ]; then
    echo "REFUSING to push — $GIT_DIR/$f exists (merge/rebase/cherry-pick in progress)"
    echo "  finish or abort it first: git status"
    exit 2
  fi
done

# Bail if the working tree is dirty — we're about to chain merges and don't
# want uncommitted work to collide with conflict markers.
if [ -n "$(git status --porcelain -- .)" ]; then
  echo "REFUSING to push — annoyance-dashboard/ has uncommitted changes"
  echo "  run ./scripts/save_session.sh <TAG> first, or stash/commit manually"
  exit 2
fi

SAVE_BRANCHES=$(git branch --list 'save/*' | sed 's/^[* ]*//' | sort)
if [ -z "$SAVE_BRANCHES" ]; then
  echo "no save/* branches found"
  exit 0
fi

echo "Will merge these saves into $TARGET:"
echo "$SAVE_BRANCHES" | sed 's/^/  - /'
printf "\nContinue? [y/N] "
read -r confirm
[ "$confirm" = "y" ] || [ "$confirm" = "Y" ] || { echo "aborted"; exit 0; }

# Create or reset the polish branch from base
git fetch origin
if git show-ref --verify --quiet "refs/heads/$TARGET"; then
  git checkout "$TARGET"
else
  git checkout -B "$TARGET" "$BASE"
fi

# Merge each save branch in turn. Process substitution (not a pipe) so
# `exit` actually terminates the script on conflict — a plain `| while`
# runs in a subshell where `exit 3` only kills the subshell and lets the
# script fall through to `git push` with a half-merged tree.
while read -r sb; do
  [ -z "$sb" ] && continue
  echo ">> merging $sb"
  if ! git merge --no-ff --no-edit "$sb"; then
    echo ""
    echo "⚠️  conflict merging $sb"
    echo "   Resolve the conflicts, then run:"
    echo "     git merge --continue"
    echo "     $0"
    exit 3
  fi
done < <(echo "$SAVE_BRANCHES")

# Push to origin
git push -u origin "$TARGET"

# Optional cleanup — also uses process substitution to avoid the subshell
# pitfall if branch deletion ever fails mid-loop.
printf "\nDelete merged save/* branches? [y/N] "
read -r cleanup
if [ "$cleanup" = "y" ] || [ "$cleanup" = "Y" ]; then
  while read -r sb; do
    [ -z "$sb" ] && continue
    git branch -D "$sb"
  done < <(echo "$SAVE_BRANCHES")
fi

echo ""
echo "✅ all saves pushed to origin/$TARGET"
echo "   tip: $(git rev-parse --short HEAD)"
