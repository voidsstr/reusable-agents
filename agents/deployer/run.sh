#!/usr/bin/env bash
# Thin wrapper so callers (seo-implementer, dashboard, cron) can invoke
# the deployer with a consistent interface.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/deployer.py" "$@"
