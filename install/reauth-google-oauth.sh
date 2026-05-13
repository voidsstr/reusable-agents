#!/usr/bin/env bash
# Re-auth Google OAuth for the seo-opportunity-agent collector.
#
# Why: the refresh_token at ~/.reusable-agents/seo/.oauth.json expires
# every ~6 months (Google policy for unused or test-tier apps). When
# that happens, the seo-opportunity-agent's collector fails with:
#   "Token refresh failed: HTTP Error 400: Bad Request"
# and no GSC/GA4 data flows. This script re-runs the OAuth consent
# flow against the same Google Cloud project, preserving the existing
# client_id + client_secret (which DO NOT expire — only the
# refresh_token does).
#
# Run this from a graphical session on the host that has the OAuth
# file (NOT over SSH-only — the OAuth flow opens a browser tab and
# needs a localhost callback that the calling session can reach).
#
# Scopes granted (must match the agent's needs):
#   - analytics.readonly  (GA4)
#   - webmasters         (GSC — read + Sitemaps.submit)
#   - adwords            (Google Ads — graceful skip if user denies)
#
# Usage:
#   bash install/reauth-google-oauth.sh
#
# Optional env overrides:
#   OAUTH_FILE=/path/to/.oauth.json   (default: ~/.reusable-agents/seo/.oauth.json)

set -euo pipefail

OAUTH_FILE="${OAUTH_FILE:-$HOME/.reusable-agents/seo/.oauth.json}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REFRESH_SCRIPT="$REPO_ROOT/agents/seo-opportunity-agent/lib/collector/refresh-token.py"

if [ ! -f "$REFRESH_SCRIPT" ]; then
    echo "ERROR: refresh-token.py not found at $REFRESH_SCRIPT" >&2
    exit 2
fi

# ── 1. Source the OAuth client credentials ──
# Priority order:
#   1. Env-var override (use a freshly-created Desktop OAuth client)
#   2. Existing $OAUTH_FILE (re-use creds from a prior bootstrap)
#
# IMPORTANT: the SEO agent needs a DESKTOP OAuth client. Mobile/Android/
# iOS clients won't work — they use a different audience and won't
# return a refresh_token to InstalledAppFlow. If you've been using a
# mobile-app OAuth client by mistake, the consent screen will reject
# you with "GrocerAI has not completed the Google verification process"
# even after adding test users — the verification status applies
# per-client-type, not per-project.
#
# Create a fresh Desktop client at:
#   https://console.cloud.google.com/apis/credentials
#     → CREATE CREDENTIALS → OAuth client ID → Application type: Desktop app
# Then either:
#   (a) Re-run this script with env vars set:
#         SEO_AGENT_CLIENT_ID=<new-id> \
#         SEO_AGENT_CLIENT_SECRET=<new-secret> \
#         bash install/reauth-google-oauth.sh
#   (b) Or hand-edit $OAUTH_FILE to swap in the new client_id + secret,
#       then run this script with no env vars set.

if [ -n "${SEO_AGENT_CLIENT_ID:-}" ] && [ -n "${SEO_AGENT_CLIENT_SECRET:-}" ]; then
    CLIENT_ID="$SEO_AGENT_CLIENT_ID"
    CLIENT_SECRET="$SEO_AGENT_CLIENT_SECRET"
    echo "Using OAuth client from env vars (client_id ${CLIENT_ID:0:30}…)"
elif [ -f "$OAUTH_FILE" ]; then
    CLIENT_ID=$(python3 -c "import json; d=json.load(open('$OAUTH_FILE')); print(d.get('client_id') or '')")
    CLIENT_SECRET=$(python3 -c "import json; d=json.load(open('$OAUTH_FILE')); print(d.get('client_secret') or '')")
    if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
        echo "ERROR: existing $OAUTH_FILE is missing client_id or client_secret" >&2
        exit 4
    fi
    echo "Using OAuth client from existing $OAUTH_FILE (client_id ${CLIENT_ID:0:30}…)"
    echo ""
    echo "  ⚠ If the consent screen previously rejected you with"
    echo "    'GrocerAI has not completed the Google verification process',"
    echo "    that client may be a MOBILE OAuth client (wrong type for this"
    echo "    agent). Create a new DESKTOP OAuth client and re-run with:"
    echo "      SEO_AGENT_CLIENT_ID=<new-id> \\"
    echo "      SEO_AGENT_CLIENT_SECRET=<new-secret> \\"
    echo "      bash install/reauth-google-oauth.sh"
    echo ""
else
    echo "ERROR: No OAuth credentials available." >&2
    echo "" >&2
    echo "Set up a Desktop OAuth client:" >&2
    echo "  1. Create at https://console.cloud.google.com/apis/credentials" >&2
    echo "     (Application type: Desktop app)" >&2
    echo "  2. Enable Search Console API + Google Analytics Data API" >&2
    echo "  3. Re-run with env vars:" >&2
    echo "       SEO_AGENT_CLIENT_ID=<id> SEO_AGENT_CLIENT_SECRET=<secret> \\" >&2
    echo "         bash install/reauth-google-oauth.sh" >&2
    exit 3
fi

# ── 2. Back up the old oauth file ──
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="${OAUTH_FILE}.expired.${TIMESTAMP}.bak"
cp -p "$OAUTH_FILE" "$BACKUP"
echo "Backed up old oauth file → $BACKUP"

# ── 3. Run the bootstrap consent flow ──
# This opens a browser tab, you sign in to Google, grant the requested
# scopes, and Google redirects back to a localhost port the flow opens.
# The script captures the auth code and exchanges it for a fresh
# refresh_token (which is what was missing/expired).
echo ""
echo "Opening browser for Google OAuth consent…"
echo "  - Sign in with the same Google account that owns the GSC + GA4 properties"
echo "  - Grant ALL requested scopes (analytics + webmasters + adwords)"
echo "  - The flow auto-redirects back to a localhost port when done"
echo ""

SEO_AGENT_CLIENT_ID="$CLIENT_ID" \
SEO_AGENT_CLIENT_SECRET="$CLIENT_SECRET" \
python3 "$REFRESH_SCRIPT" --oauth-file "$OAUTH_FILE" --bootstrap

# ── 4. Verify the new token works ──
echo ""
echo "Verifying new refresh_token by minting an access token…"
NEW_TOKEN=$(python3 "$REFRESH_SCRIPT" --oauth-file "$OAUTH_FILE")
if [ -z "$NEW_TOKEN" ]; then
    echo "ERROR: refresh failed even after bootstrap — check the OAuth flow output above" >&2
    exit 5
fi
echo "  ✓ Got fresh access token (${#NEW_TOKEN} chars)"

# ── 5. Quick GSC sanity check ──
echo ""
echo "Testing GSC API with new token (lists registered sites)…"
SITES=$(curl -sS -H "Authorization: Bearer $NEW_TOKEN" \
    "https://searchconsole.googleapis.com/webmasters/v3/sites" 2>&1)
if echo "$SITES" | python3 -c "import json,sys; d=json.load(sys.stdin); print('  ✓ GSC sites accessible:'); [print(f\"    - {e.get(\\\"siteUrl\\\")}\") for e in (d.get('siteEntry') or [])[:6]]" 2>/dev/null; then
    :
else
    echo "  ⚠ GSC API rejected the token — check that the OAuth account owns these properties:"
    echo "    https://aisleprompt.com/"
    echo "    https://specpicks.com/"
    echo "    (or sc-domain: variants)"
fi

echo ""
echo "OAuth re-authorized at $OAUTH_FILE"
echo "Next seo-opportunity-agent run will pick up the fresh token automatically."
