#!/usr/bin/env bash
# aws/provision.sh: provision this site's resources (phases: see --help) on ECS Fargate,
# with this site's deploy.env. Every argument goes to the vendored kit script
# (a later --env wins); docs: vendor/affiliate-kit/deploy/README.md.
#   bash aws/provision.sh --dry-run ...    print every command instead of running it
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "$here/../vendor/affiliate-kit/deploy/aws/provision.sh" --env "$here/../deploy.env" "$@"
