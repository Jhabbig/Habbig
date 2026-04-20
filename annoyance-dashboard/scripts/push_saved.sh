#!/usr/bin/env bash
# Merge all save/* branches into feature/annoyance-polish and push.
# Run once, when you're ready to ship the whole stack of save_session.sh runs.

set -euo pipefail

REPO_ROOT="/Users/shocakarel/Habbig/annoyance-dashboard"
TARGET="feature/annoyance-polish"
BASE="feature/platform-build"

cd "$REPO_ROOT"

SAVE_BRANCHES=$(git branch --list 'save/*' | sed 's/^[* ]*//' | sort)
if [ -z "$SAVE_BRANCHES" ]; then
  echo "no save/* branches found"
  exit 0
fi

echo "Will merge these saves into $TARGET:"
echo "$SAVE_BRANCHES" | sed 's/^/  - /'
printf "\nContinue? [y/N] "
read -r confirm
[ "$confirm" = "y" ] || { echo "aborted"; exit 0; }

# Create or reset the polish branch from base
git fetch origin
if git show-ref --verify --quiet "refs/heads/$TARGET"; then
  git checkout "$TARGET"
else
  git checkout -B "$TARGET" "$BASE"
fi

# Merge each save branch in turn
echo "$SAVE_BRANCHES" | while read -r sb; do
  echo ">> merging $sb"
  if ! git merge --no-ff --no-edit "$sb"; then
    echo "⚠️  conflict merging $sb — resolve, then run: git merge --continue && $0"
    exit 3
  fi
done

# Push to origin
git push -u origin "$TARGET"

# Optional cleanup
printf "\nDelete merged save/* branches? [y/N] "
read -r cleanup
if [ "$cleanup" = "y" ]; then
  echo "$SAVE_BRANCHES" | xargs -n1 git branch -D
fi

echo "✅ all saves pushed to origin/$TARGET"
