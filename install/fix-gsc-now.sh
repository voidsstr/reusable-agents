#!/usr/bin/env bash
# One-shot GSC re-auth fixer.
# Kills any stale reauth/consent flows, runs a fresh interactive Google OAuth
# consent (opens your browser), then verifies + restarts the SEO agents.
#
# RUN THIS IN YOUR TERMINAL (needs a graphical session — it opens a browser tab).
#   bash /home/voidsstr/development/reusable-agents/install/fix-gsc-now.sh
set -uo pipefail

REPO=/home/voidsstr/development/reusable-agents
OAUTH=~/.reusable-agents/seo/.oauth.json
REFRESH="$REPO/agents/seo-opportunity-agent/lib/collector/refresh-token.py"

echo "== 1. Killing stale reauth / consent flows =="
pkill -f 'refresh-token.py .*--bootstrap' 2>/dev/null && echo "  killed stale bootstrap flow(s)" || echo "  none running"
sleep 1

echo "== 2. Current refresh-token status (should be HTTP 400 before fix) =="
python3 "$REFRESH" --oauth-file "$OAUTH" 2>&1 | head -1

echo "== 3. Launching FRESH consent flow — sign in with the Google account that"
echo "      owns the aisleprompt + specpicks GSC/GA4 properties, grant ALL scopes."
echo "      (If it warns 'GrocerAI hasn't completed verification' -> Advanced ->"
echo "       Go to GrocerAI (unsafe); it's your own project.)"
echo
bash "$REPO/install/reauth-google-oauth.sh"
RC=$?
if [ $RC -ne 0 ]; then
  echo "!! reauth exited $RC — consent not completed. Re-run this script." >&2
  exit $RC
fi

echo "== 4. Verifying new token =="
TOK=$(python3 "$REFRESH" --oauth-file "$OAUTH" 2>&1 | head -c 40)
case "$TOK" in
  ya29*) echo "  OK — minted access token." ;;
  *) echo "!! token still bad: $TOK" >&2; exit 5 ;;
esac

echo "== 5. Restarting SEO + oauth-heartbeat agents =="
systemctl --user start agent-aisleprompt-seo-opportunity-agent.service
systemctl --user start agent-oauth-heartbeat-agent.service
systemctl --user start agent-specpicks-seo-opportunity-agent.service 2>/dev/null || true
sleep 40
for u in agent-aisleprompt-seo-opportunity-agent agent-oauth-heartbeat-agent; do
  printf '  %-45s %s\n' "$u" "$(systemctl --user is-failed $u.service 2>/dev/null || echo cleared)"
done

echo "== DONE — GSC re-authed. The keep-the-lights-on loop will confirm on its next tick. =="
