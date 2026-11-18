#!/usr/bin/env bash
# aws/deploy.sh: build, gate, push, migrate, release and smoke-check this site on ECS Fargate,
# with this site's deploy.env. Every argument goes to the vendored kit script
# (a later --env wins); docs: vendor/affiliate-kit/deploy/README.md.
#   bash aws/deploy.sh --dry-run ...    print every command instead of running it
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "$here/../vendor/affiliate-kit/deploy/aws/deploy.sh" --env "$here/../deploy.env" "$@"
