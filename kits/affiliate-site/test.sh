#!/usr/bin/env bash
# Kit test entry point: type-check, then the jest unit suite.
#
#   ./test.sh                  typecheck + all unit tests
#   ./test.sh unit/price.test.ts   typecheck + one file (args go to jest)
#
# Never installs anything: run `npm ci` first (the lockfile pins every kit
# item's dependencies). Exit code is non-zero on any type error or test failure.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -x node_modules/.bin/jest || ! -x node_modules/.bin/tsc ]]; then
  echo "affiliate-site kit: dependencies missing; run 'npm ci' in $(pwd)" >&2
  exit 2
fi

node_major="$(node -p 'process.versions.node.split(".")[0]')"
if (( node_major < 22 )); then
  echo "affiliate-site kit: node >= 22 required (found $(node --version))" >&2
  exit 2
fi

npm run --silent typecheck
exec node_modules/.bin/jest "$@"
