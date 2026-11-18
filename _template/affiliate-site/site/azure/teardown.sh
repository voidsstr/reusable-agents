#!/usr/bin/env bash
# azure/teardown.sh: remove this site's app (shared resources are never touched) on Azure Container Apps,
# with this site's deploy.env. Every argument goes to the vendored kit script
# (a later --env wins); docs: vendor/affiliate-kit/deploy/README.md.
#   bash azure/teardown.sh --dry-run ...    print every command instead of running it
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "$here/../vendor/affiliate-kit/deploy/azure/teardown.sh" --env "$here/../deploy.env" "$@"
