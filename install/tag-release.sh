#!/usr/bin/env bash
# Tag a deployed commit with a monotonically incrementing release number.
#
#   tag-release.sh <site> [image-tag]
#
# Produces `release/<site>/NNNN` (zero-padded, starting at 0001) on HEAD and
# pushes the tag. Answers "what went live, and when" from `git tag` alone —
# which timestamped tags do not, because you cannot tell ordering at a glance
# or say "we are three releases past the one that broke it".
#
# Deliberately tags but does NOT push the branch: on a diverged branch a push
# would fail or, worse, need a force. The tag records the deployed commit even
# when the branch cannot fast-forward, which is exactly the situation this
# fleet has been in.
set -uo pipefail

SITE="${1:?usage: tag-release.sh <site> [image-tag]}"
IMAGE_TAG="${2:-}"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "[tag-release] not a git repo"; exit 0; }
cd "$REPO_ROOT"

git fetch --tags origin --quiet 2>/dev/null || true

# Highest existing number for this site, local or remote.
LAST=$(git tag -l "release/${SITE}/*" | sed "s#release/${SITE}/##" | grep -E '^[0-9]+$' | sort -n | tail -1)
# 10# forces base 10. Without it bash reads a leading-zero number as OCTAL,
# so release 0008 -- the first zero-padded value that is not valid octal --
# died with "value too great for base (error token is 0008)" and tagging
# stopped dead at 8. 0001-0007 worked, which is exactly the kind of bug that
# ships fine and fails a week later. 0009 would fail too.
NEXT=$(printf "%04d" $(( 10#${LAST:-0} + 1 )))
TAG="release/${SITE}/${NEXT}"

SHA=$(git rev-parse --short HEAD)
MSG="release ${NEXT} — ${SITE} — ${SHA}${IMAGE_TAG:+ — image ${IMAGE_TAG}}"

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "[tag-release] ${TAG} already exists; nothing to do"
  exit 0
fi

git tag -a "${TAG}" -m "${MSG}" || { echo "[tag-release] could not create ${TAG}"; exit 0; }
echo "[tag-release] created ${TAG} on ${SHA}"

# A failed tag push must never fail a deploy that already succeeded.
if git push origin "refs/tags/${TAG}" --quiet 2>/dev/null; then
  echo "[tag-release] pushed ${TAG}"
else
  echo "[tag-release] WARNING: ${TAG} created locally but could not be pushed"
fi
