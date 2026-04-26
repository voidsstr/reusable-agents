#!/usr/bin/env bash
# Install the host-worker as a systemd --user service so dashboard "Run now"
# triggers fire on the host (where docker, az, git, ssh are available).
#
# Idempotent — safe to re-run after an update of the worker script.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKER_SCRIPT="$REPO_DIR/framework/api/host-worker.sh"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_NAME="reusable-agents-host-worker.service"

if [ ! -x "$WORKER_SCRIPT" ]; then
    chmod +x "$WORKER_SCRIPT"
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$UNIT_NAME" <<UNIT
[Unit]
Description=reusable-agents host-worker (executes dashboard-triggered agent runs on the host)
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
ExecStart=/bin/bash $WORKER_SCRIPT
Restart=always
RestartSec=5
StandardOutput=append:/tmp/reusable-agents-host-worker.log
StandardError=append:/tmp/reusable-agents-host-worker.log

[Install]
WantedBy=default.target
UNIT

echo "Wrote $UNIT_DIR/$UNIT_NAME"

# Enable lingering so user services survive logout
loginctl enable-linger "$USER" 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

sleep 1
systemctl --user status "$UNIT_NAME" --no-pager 2>&1 | head -10 || true
echo ""
echo "Host-worker active. Logs: tail -f /tmp/reusable-agents-host-worker.log"
