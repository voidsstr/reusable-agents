#!/usr/bin/env bash
# Re-authenticate claude-pool profiles whose OAuth session has expired.
#
#   bash install/reauth-claude-pool.sh              # every profile that needs it
#   bash install/reauth-claude-pool.sh 1 2 4        # just these slots
#   bash install/reauth-claude-pool.sh --all        # every profile, healthy or not
#
# A profile "needs it" when state.json marks it unauthenticated, when it carries
# an auth_error_message (e.g. "OAuth session expired and could not be
# refreshed"), or when its .claude/.credentials.json is missing. Note that
# `authenticated: true` alone is NOT proof of health: the pool keeps that flag
# set on profiles whose refresh already failed, so this script keys off the
# error field too.
#
# For each profile it runs the interactive `claude /login` under that profile's
# HOME (complete the browser flow, then type /exit or Ctrl-D to return here),
# confirms the credentials file was actually rewritten, and only then clears the
# error in state.json under the pool's own lock. The pool picks the profile up on
# its next pick — no service restart needed.
#
# Deliberately does NOT verify with `claude --print`: probing a pool profile
# that way can blank its tokens. The credentials-file mtime is the check.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
POOL_DIR="${CLAUDE_POOL_DIR:-$HOME/.reusable-agents/claude-pool}"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
STATE="$POOL_DIR/state.json"

[ -x "$CLAUDE_BIN" ] || { echo "claude binary not found at $CLAUDE_BIN (set CLAUDE_BIN)"; exit 1; }
[ -f "$STATE" ] || { echo "no pool state at $STATE"; exit 1; }

ALL=0; SLOTS=()
for a in "$@"; do
    case "$a" in
        --all) ALL=1 ;;
        -h|--help) sed -n 2,21p "$0"; exit 0 ;;
        *) SLOTS+=("profile-${a#profile-}") ;;
    esac
done

# Profiles to process: explicit slots, or everything that looks unhealthy.
mapfile -t TARGETS < <(python3 - "$STATE" "$ALL" "${SLOTS[@]}" <<'PY'
import json, os, sys
state = json.load(open(sys.argv[1])); all_ = sys.argv[2] == "1"; wanted = sys.argv[3:]
for pid, p in sorted(state.items()):
    if not isinstance(p, dict) or not pid.startswith("profile-"):
        continue
    creds = os.path.join(p.get("home", ""), ".claude", ".credentials.json")
    unhealthy = (not p.get("authenticated")) or p.get("auth_error_message") or not os.path.exists(creds)
    if (wanted and pid in wanted) or (not wanted and (all_ or unhealthy)):
        print(pid)
PY
)

if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "Every profile looks healthy. Use --all or name slots to force a re-login."
    exit 0
fi

echo "Profiles to re-authenticate: ${TARGETS[*]}"
OK=(); SKIPPED=()
for pid in "${TARGETS[@]}"; do
    home="$POOL_DIR/$pid"
    creds="$home/.claude/.credentials.json"
    label=$(python3 -c "import json,sys; print(json.load(open('$STATE')).get('$pid',{}).get('label') or '(no label)')")
    before=$(stat -c %Y "$creds" 2>/dev/null || echo 0)

    echo
    echo "════════════════════════════════════════════════════════════════"
    echo " $pid   account: $label"
    echo " Log in with THAT account in the browser, then /exit to continue."
    echo "════════════════════════════════════════════════════════════════"
    read -r -p " Press Enter to start (s = skip): " ans
    if [ "${ans:-}" = "s" ]; then SKIPPED+=("$pid"); continue; fi

    HOME="$home" "$CLAUDE_BIN" /login || true

    after=$(stat -c %Y "$creds" 2>/dev/null || echo 0)
    if [ "$after" -le "$before" ]; then
        echo " ✗ $pid: credentials were not rewritten — login did not complete. Left marked unhealthy."
        SKIPPED+=("$pid"); continue
    fi

    PYTHONPATH="$REPO_DIR" python3 - "$pid" <<'PY'
import sys
from framework.cli import claude_pool as cp
pid = sys.argv[1]
fd = cp._open_state_locked()
try:
    state = cp._read_state(fd)
    p = state.get(pid)
    if p is not None:
        p["authenticated"] = True
        for k in ("auth_error_at", "auth_error_message"):
            p.pop(k, None)
        cp._write_state(fd, state)
finally:
    cp._close_state(fd)
print(f" ✓ {pid}: re-authenticated, error cleared in state.json")
PY
    OK+=("$pid")
done

echo
echo "Re-authenticated: ${OK[*]:-none}"
[ ${#SKIPPED[@]} -gt 0 ] && echo "Skipped / not completed: ${SKIPPED[*]}"
echo
PYTHONPATH="$REPO_DIR" python3 -m framework.cli.claude_pool status 2>/dev/null || true
