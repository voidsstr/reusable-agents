#!/usr/bin/env bash
# agents-pause.sh — stop every agent timer and the services that spend
# Claude credits, recording exactly what was running so the resume can put
# back that set and nothing more.
#
#   bash install/agents-pause.sh
#   bash install/agents-resume.sh
#
# Why a state file rather than "disable all / enable all": some units are
# disabled ON PURPOSE. specpicks-scraper-watchdog is `enabled: false` in its
# manifest and in the registry, and a blanket re-enable would resurrect it --
# which is the exact bug fixed in a9ca035, where registration kept switching
# it back on and it failed every five minutes for weeks. Resume replays the
# recorded list, so a unit that was off stays off.
set -uo pipefail

STATE_DIR="${STATE_DIR:-$HOME/.reusable-agents/pause-state}"
mkdir -p "$STATE_DIR"
TIMERS_FILE="$STATE_DIR/paused-timers.txt"
SERVICES_FILE="$STATE_DIR/paused-services.txt"

# Long-running services that consume Claude credits or dispatch work.
SERVICES=(auto-queue-drainer.service retro-chat-brain.service)

echo "==> recording what is currently enabled"
systemctl --user list-unit-files --no-pager 2>/dev/null \
  | awk '/^agent-.*\.timer/ && $2 == "enabled" { print $1 }' > "$TIMERS_FILE"
: > "$SERVICES_FILE"
for s in "${SERVICES[@]}"; do
    if [ "$(systemctl --user is-active "$s" 2>/dev/null)" = "active" ]; then
        echo "$s" >> "$SERVICES_FILE"
    fi
done
echo "    $(wc -l < "$TIMERS_FILE") timers, $(wc -l < "$SERVICES_FILE") services -> $STATE_DIR"

if [ ! -s "$TIMERS_FILE" ] && [ ! -s "$SERVICES_FILE" ]; then
    echo "==> nothing was running; leaving the previous state file alone"
    exit 0
fi

echo "==> stopping agent timers"
stopped=0
while read -r t; do
    [ -n "$t" ] || continue
    systemctl --user stop "$t" >/dev/null 2>&1
    systemctl --user disable "$t" >/dev/null 2>&1 && stopped=$((stopped + 1))
done < "$TIMERS_FILE"
echo "    stopped+disabled $stopped"

echo "==> stopping credit-spending services"
while read -r s; do
    [ -n "$s" ] || continue
    systemctl --user stop "$s" >/dev/null 2>&1 && echo "    stopped $s"
done < "$SERVICES_FILE"

# A running agent keeps spending after its timer is gone, so kill in-flight
# work too -- that is the whole point of pausing.
echo "==> stopping any in-flight agent runs"
for u in $(systemctl --user list-units --state=running --no-pager 2>/dev/null \
             | awk '/^ *agent-.*\.service/ { print $1 }'); do
    systemctl --user stop "$u" >/dev/null 2>&1 && echo "    stopped $u"
done
pkill -f 'framework.cli.claude_pool' 2>/dev/null && echo "    killed claude_pool workers"

echo
echo "PAUSED. Resume with:  bash install/agents-resume.sh"
