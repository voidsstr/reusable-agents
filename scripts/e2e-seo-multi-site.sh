#!/usr/bin/env bash
# Run the E2E SEO test against multiple sites sequentially.
#
# For each site, in order:
#   1. Trigger that site's SEO agent via systemctl --user start
#   2. Wait for the outbound recommendations email to be recorded in Azure
#   3. Synthesize an "implement all changes" reply via Microsoft Graph
#   4. Watch the responder + implementer + deployer chain to a terminal state
#   5. Move to the next site
#
# Usage:
#   bash scripts/e2e-seo-multi-site.sh aisleprompt specpicks
#   bash scripts/e2e-seo-multi-site.sh aisleprompt
#
# Logs:
#   /tmp/e2e-seo-test-<site>.log    one log per site test
#   /tmp/e2e-seo-multi-site.log     wrapper-level summary

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WRAPPER_LOG="/tmp/e2e-seo-multi-site.log"
exec > >(tee -a "$WRAPPER_LOG") 2>&1

SITES=("$@")
if [ ${#SITES[@]} -eq 0 ]; then
    echo "usage: $0 <site> [site...]" >&2
    exit 2
fi

stamp() { date -u +%H:%M:%SZ; }

for site in "${SITES[@]}"; do
    echo
    echo "════════════════════════════════════════════════════════════════════"
    echo "[$(stamp)] ▶ START site=$site"
    echo "════════════════════════════════════════════════════════════════════"

    # Resolve the agent service name. Per-site agents are <site>-seo-opportunity-agent.
    SVC="agent-${site}-seo-opportunity-agent.service"

    # Capture "now" so the test script only matches emails sent AFTER trigger.
    SINCE=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
    echo "[$(stamp)] · since=$SINCE"

    # Start the SEO agent. Type=oneshot so this returns when the agent FINISHES.
    # We wait for that — the test script polls Azure for the outbound email.
    echo "[$(stamp)] · triggering $SVC (this can take 5-30 min)"
    systemctl --user start "$SVC" &
    SVC_PID=$!

    # Run the E2E test in foreground. It polls until the email is sent, then
    # auto-replies, then watches the chain to terminal.
    SITE_LOG="/tmp/e2e-seo-test-${site}.log"
    : > "$SITE_LOG"
    python3 "$REPO/scripts/e2e-seo-implement-test.py" \
        --site "$site" \
        --since "$SINCE" \
        --email-wait-min 45 \
        --chain-wait-min 90 \
        2>&1 | tee -a "$SITE_LOG"
    rc=$?
    echo "[$(stamp)] · e2e rc=$rc — site=$site"

    # Wait for systemctl --start to wrap up (it should be done by now since
    # the email-wait blocked until the SEO agent's reporter step finished).
    wait "$SVC_PID" 2>/dev/null || true

    if [ $rc -ne 0 ]; then
        echo "[$(stamp)] ✗ site=$site failed rc=$rc — continuing to next site"
    else
        echo "[$(stamp)] ✓ site=$site complete"
    fi
done

echo
echo "════════════════════════════════════════════════════════════════════"
echo "[$(stamp)] ALL SITES PROCESSED — see /tmp/e2e-seo-test-*.log per site"
echo "════════════════════════════════════════════════════════════════════"
