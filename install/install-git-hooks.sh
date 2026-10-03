#!/usr/bin/env bash
# Link the fleet's git hooks into every repo the agents commit to.
#
#   bash install/install-git-hooks.sh [repo-dir ...]
#
# Default repos: reusable-agents plus the sibling site and assistant repos.
# Idempotent; leaves an existing non-symlink hook alone and says so.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV="$(cd "$HERE/../.." && pwd)"
HOOK="$HERE/git-hooks/pre-commit-no-secrets"
chmod +x "$HOOK"
if [ "$#" -gt 0 ]; then
  REPOS=("$@")
else
  REPOS=("$DEV/reusable-agents" "$DEV/specpicks" "$DEV/aisleprompt" "$DEV/nsc-assistant")
fi
for repo in "${REPOS[@]}"; do
  if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    echo "skip $repo (not a git repo)"
    continue
  fi
  hooks="$(git -C "$repo" rev-parse --git-path hooks)"
  case "$hooks" in /*) ;; *) hooks="$repo/$hooks" ;; esac
  mkdir -p "$hooks"
  dest="$hooks/pre-commit"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    echo "skip $repo: a non-symlink pre-commit hook exists ($dest)"
    continue
  fi
  ln -sfn "$HOOK" "$dest"
  echo "linked $dest -> $HOOK"
done
