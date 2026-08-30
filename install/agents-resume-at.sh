#!/usr/bin/env bash
# Schedule agents-resume.sh for a given local time, as a one-shot systemd
# transient timer that survives this shell and reboots of the user session.
#
#   bash install/agents-resume-at.sh "2026-08-31 23:00:00"
#
# Uses LOCAL time. Check with: systemctl --user list-timers agents-resume-once
set -uo pipefail
WHEN="${1:?usage: agents-resume-at.sh \"YYYY-MM-DD HH:MM:SS\" (local time)}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

systemctl --user stop agents-resume-once.timer  >/dev/null 2>&1 || true
systemctl --user reset-failed agents-resume-once.service >/dev/null 2>&1 || true

systemd-run --user \
    --unit=agents-resume-once \
    --on-calendar="$WHEN" \
    --timer-property=AccuracySec=1min \
    --timer-property=Persistent=true \
    --description="Resume reusable-agents at $WHEN" \
    /bin/bash "$REPO_ROOT/install/agents-resume.sh"

echo
systemctl --user list-timers agents-resume-once.timer --all --no-pager
