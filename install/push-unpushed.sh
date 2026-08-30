#!/usr/bin/env bash
# push-unpushed.sh <repo-path>
#
# Push a site repo's current branch if it is AHEAD of its upstream and NOT
# behind. Safe to call unconditionally and repeatedly: it is a no-op when the
# branch is in sync, and it deliberately refuses to touch a diverged branch
# (that needs a human merge, not a force).
#
# Why this exists: commits reach these repos from three different paths --
# the implementer's own `git commit`, the claude code-editor agent committing
# from inside its own bash sandbox (with its own message format, which is why
# grepping run.sh for "git commit" does not find it), and the deployer. Only
# two of those pushed, so agent work accumulated locally: at its worst
# specpicks and aisleprompt were 265 commits ahead of origin, i.e. code
# running in production that existed on one machine only.
#
# A failed push must NEVER fail the caller -- the commit and the deploy still
# stand. Always exits 0.
set -u

repo="${1:-}"
[ -n "$repo" ] && [ -d "$repo/.git" ] || { echo "[push-unpushed] no git repo at '${repo}'" >&2; exit 0; }

br=$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [ -z "$br" ] || [ "$br" = "HEAD" ]; then
    echo "[push-unpushed] $repo: detached HEAD, not pushing" >&2
    exit 0
fi

git -C "$repo" fetch origin "$br" --quiet 2>/dev/null || true

counts=$(git -C "$repo" rev-list --left-right --count "origin/$br...HEAD" 2>/dev/null || echo "")
if [ -z "$counts" ]; then
    echo "[push-unpushed] $repo: no origin/$br to compare against, not pushing" >&2
    exit 0
fi
behind=$(echo "$counts" | cut -f1)
ahead=$(echo "$counts" | cut -f2)

if [ "${ahead:-0}" -eq 0 ]; then
    exit 0
fi
if [ "${behind:-0}" -ne 0 ]; then
    echo "[push-unpushed] WARNING: $repo $br is $ahead ahead and $behind behind origin -- diverged, needs a manual merge. NOT pushing." >&2
    exit 0
fi

if git -C "$repo" push origin "HEAD:$br" >/dev/null 2>&1; then
    echo "[push-unpushed] pushed $ahead commit(s) to origin/$br ($repo)" >&2
else
    echo "[push-unpushed] WARNING: push of $br failed ($ahead commit(s) still local only) ($repo)" >&2
fi
exit 0
