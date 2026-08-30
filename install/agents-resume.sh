#!/usr/bin/env bash
# agents-resume.sh — put back exactly the timers and services that
# agents-pause.sh recorded, and nothing else.
#
#   bash install/agents-resume.sh
#
# Deliberately replays the recorded list rather than enabling every agent:
# some units are disabled on purpose (see the note in agents-pause.sh), and
# a blanket enable would resurrect them.
set -uo pipefail

STATE_DIR="${STATE_DIR:-$HOME/.reusable-agents/pause-state}"
TIMERS_FILE="$STATE_DIR/paused-timers.txt"
SERVICES_FILE="$STATE_DIR/paused-services.txt"

if [ ! -f "$TIMERS_FILE" ]; then
    echo "no pause state at $TIMERS_FILE — nothing to resume"
    exit 0
fi

# /tmp is wiped on reboot and systemd cannot create the parent of an
# `append:` StandardOutput target, so without this every unit fails at
# 209/STDOUT before running a line of agent code (see 50d4979).
mkdir -p /tmp/reusable-agents-logs

echo "==> re-enabling agent timers"
started=0
while read -r t; do
    [ -n "$t" ] || continue
    if systemctl --user enable --now "$t" >/dev/null 2>&1; then
        started=$((started + 1))
    else
        echo "    WARNING: could not enable $t"
    fi
done < "$TIMERS_FILE"
echo "    enabled+started $started of $(wc -l < "$TIMERS_FILE")"

echo "==> restarting services"
if [ -f "$SERVICES_FILE" ]; then
    while read -r s; do
        [ -n "$s" ] || continue
        systemctl --user start "$s" >/dev/null 2>&1 \
            && echo "    started $s" \
            || echo "    WARNING: could not start $s"
    done < "$SERVICES_FILE"
fi

mv "$TIMERS_FILE" "$TIMERS_FILE.resumed-$(date -u +%Y%m%dT%H%M%SZ)" 2>/dev/null || true
echo
echo "RESUMED."
