#!/usr/bin/env bash
# The gate fixture's deploy gate: the kit runner with this fixture's suite.env.
# A site's own shim is the same two lines, pointing at vendor/affiliate-kit.
#   bash fixture-site/tests/run-local-suite.sh [PLAYWRIGHT ARGS...]
#   SUITE_ENV=fixture-site/tests/suite.violations.env bash fixture-site/tests/run-local-suite.sh
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "$here/../../tests/run-local-suite.sh" --suite-env "${SUITE_ENV:-$here/suite.env}" "$@"
