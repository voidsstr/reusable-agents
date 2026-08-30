#!/usr/bin/env bash
# deploy-drift.sh [site:repo-path ...]
#
# For each site repo, report how far its branch has drifted past the newest
# release/<site>/NNNN tag -- i.e. what is committed and pushed but has never
# gone live.
#
# Why: on 2026-08-30 specpicks had three commits, one of them an approved
# API change, sitting on master for ten hours past release/specpicks/0001.
# Nothing anywhere said so. Commits are visible, deploys are visible, but the
# GAP between them was not, and that gap is where finished work goes to sit.
#
# Caveat on the first readings: release tagging only started 2026-08-30, so
# the oldest tag is a floor, not a true "last deployed" marker -- everything
# before it shipped untagged. The number is meaningful from the second tag on.
#
# Read-only. Exits 1 if any repo has drifted, so it can be used as a check.
set -uo pipefail

DEFAULT_TARGETS=(
  "specpicks:/home/voidsstr/development/specpicks"
  "aisleprompt:/home/voidsstr/development/aisleprompt"
)
TARGETS=("$@")
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=("${DEFAULT_TARGETS[@]}")

drift_found=0
for t in "${TARGETS[@]}"; do
    site="${t%%:*}"
    repo="${t#*:}"
    if [ ! -d "$repo/.git" ]; then
        printf '%-14s no git repo at %s\n' "$site" "$repo"
        continue
    fi
    git -C "$repo" fetch --tags origin --quiet 2>/dev/null || true

    last=$(git -C "$repo" tag -l "release/${site}/*" \
             | sed "s#release/${site}/##" | grep -E '^[0-9]+$' | sort -n | tail -1)
    if [ -z "${last:-}" ]; then
        printf '%-14s NEVER DEPLOYED (no release/%s/* tag)\n' "$site" "$site"
        drift_found=1
        continue
    fi
    tag="release/${site}/${last}"
    n=$(git -C "$repo" rev-list --count "${tag}..HEAD" 2>/dev/null || echo "?")
    when=$(git -C "$repo" log -1 --format=%ci "$tag" 2>/dev/null)
    if [ "$n" = "0" ]; then
        printf '%-14s up to date at %s (%s)\n' "$site" "$tag" "$when"
    else
        printf '%-14s %s commit(s) NOT DEPLOYED since %s (%s)\n' "$site" "$n" "$tag" "$when"
        git -C "$repo" log --oneline "${tag}..HEAD" | head -10 | sed 's/^/                 /'
        [ "$n" -gt 10 ] 2>/dev/null && printf '                 ... and %s more\n' "$(( n - 10 ))"
        drift_found=1
    fi
done
exit $drift_found
