#!/usr/bin/env bash
# refresh-gsc-token.sh — operator-friendly wrapper around the GSC/GA4/Ads
# OAuth refresh-token at ~/.reusable-agents/seo/.oauth.json.
#
# Why this exists
# ---------------
# The SEO/GSC stack (specpicks-seo-opportunity-agent + aisleprompt-seo-
# opportunity-agent + gsc-coverage-auditor + sitemap submitter) all
# depend on one Google OAuth refresh_token. Google's Testing-mode
# OAuth apps revoke that refresh_token after 7 days of non-use, and
# any successful refresh resets the 7-day clock.
#
# Three jobs an operator does often:
#   1. "is the token still alive?"  →  refresh-gsc-token.sh status
#   2. "ping it now to reset the 7-day timer"  →  refresh-gsc-token.sh refresh
#   3. "it's dead, walk me through re-consent"  →  refresh-gsc-token.sh reauth
#
# The first two are what the oauth-heartbeat-agent does on a daily
# timer — but it's useful to run them on demand (after a pool outage,
# before a deploy, when an SEO-agent run alert fires). The third
# delegates to the existing install/reauth-google-oauth.sh.
#
# Usage
# -----
#   bash install/refresh-gsc-token.sh                 # = status (default)
#   bash install/refresh-gsc-token.sh status          # validate without printing
#   bash install/refresh-gsc-token.sh refresh         # mint a fresh access token (keeps 7-day clock alive)
#   bash install/refresh-gsc-token.sh reauth          # full re-consent flow (browser required)
#   bash install/refresh-gsc-token.sh cron            # silent on success, exit 2 on failure (for cron / heartbeat)
#
# Environment overrides
# ---------------------
#   OAUTH_FILE       Path to .oauth.json (default: ~/.reusable-agents/seo/.oauth.json)
#   QUIET=1          Suppress decorative output (status/refresh only)
#
# Exit codes
# ----------
#   0  token healthy / action succeeded
#   2  token broken or action failed — run `reauth` to recover
#   3  configuration error (oauth file missing / refresh-token.py missing)

set -euo pipefail

OAUTH_FILE="${OAUTH_FILE:-$HOME/.reusable-agents/seo/.oauth.json}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REFRESH_SCRIPT="$REPO_ROOT/agents/seo-opportunity-agent/lib/collector/refresh-token.py"
REAUTH_SCRIPT="$REPO_ROOT/install/reauth-google-oauth.sh"
QUIET="${QUIET:-0}"

CMD="${1:-status}"

say() { [ "$QUIET" = "1" ] || echo "$@"; }
warn() { echo "$@" >&2; }

if [ ! -f "$REFRESH_SCRIPT" ]; then
    warn "ERROR: refresh-token.py not found at $REFRESH_SCRIPT"
    warn "This script must be run from inside the reusable-agents repo."
    exit 3
fi

case "$CMD" in
    status|check)
        if [ ! -f "$OAUTH_FILE" ]; then
            warn "❌ No oauth file at $OAUTH_FILE"
            warn "   Run:  bash install/refresh-gsc-token.sh reauth"
            exit 3
        fi
        say "→ Checking $OAUTH_FILE"
        if OUTPUT=$(python3 "$REFRESH_SCRIPT" --oauth-file "$OAUTH_FILE" --check 2>&1); then
            say "✅ Token healthy ($OUTPUT)"
            exit 0
        else
            warn "❌ Token broken — $OUTPUT"
            warn "   This usually means the refresh_token was revoked"
            warn "   (Google's Testing-mode 7-day idle expiry)."
            warn "   To recover, run:"
            warn "     bash install/refresh-gsc-token.sh reauth"
            exit 2
        fi
        ;;

    refresh|ping|heartbeat)
        # Mints a fresh 1-hour access token, which is what resets
        # Google's 7-day inactivity clock on the refresh_token.
        # Same operation the oauth-heartbeat-agent performs daily.
        if [ ! -f "$OAUTH_FILE" ]; then
            warn "❌ No oauth file at $OAUTH_FILE"
            warn "   Run:  bash install/refresh-gsc-token.sh reauth"
            exit 3
        fi
        say "→ Refreshing $OAUTH_FILE (resets Google's 7-day clock)"
        if TOKEN=$(python3 "$REFRESH_SCRIPT" --oauth-file "$OAUTH_FILE" 2>&1); then
            say "✅ Got fresh access token (${#TOKEN} chars) — 7-day clock reset"
            exit 0
        else
            warn "❌ Refresh failed — $TOKEN"
            warn "   The refresh_token is likely revoked. Run:"
            warn "     bash install/refresh-gsc-token.sh reauth"
            exit 2
        fi
        ;;

    cron)
        # Silent-on-success heartbeat mode. Suitable for cron, systemd
        # timer, or pre-deploy gate. Exits 2 on failure so cron MAILTO
        # surfaces it without spam on healthy runs.
        if ! python3 "$REFRESH_SCRIPT" --oauth-file "$OAUTH_FILE" --check >/dev/null 2>&1; then
            warn "GSC OAuth token at $OAUTH_FILE is broken — run install/refresh-gsc-token.sh reauth"
            exit 2
        fi
        exit 0
        ;;

    reauth|bootstrap|re-auth)
        # Delegate to the existing bootstrap helper. Don't duplicate
        # the consent-flow / backup / GSC-sanity-check logic — that
        # script already does it correctly.
        if [ ! -f "$REAUTH_SCRIPT" ]; then
            warn "ERROR: reauth-google-oauth.sh not found at $REAUTH_SCRIPT"
            exit 3
        fi

        # Headless detection — the consent flow opens a browser tab
        # and needs a localhost callback. On an SSH-only / headless
        # box without forwarded ports this won't work. Flag it so
        # the operator doesn't burn a consent on a doomed flow.
        if [ -z "${DISPLAY:-}" ] && [ -z "${SSH_TTY:-}" ] && [ ! -e /dev/tty ]; then
            warn "⚠ No DISPLAY and no TTY — the OAuth consent flow needs"
            warn "  either a local browser OR an SSH session with port"
            warn "  forwarding (-L 8080:localhost:8080-ish) so the"
            warn "  callback can reach your browser. Re-run from a"
            warn "  graphical session or with port-forwarded SSH."
            exit 3
        fi
        if [ -z "${DISPLAY:-}" ]; then
            warn "ℹ No \$DISPLAY set — if you're SSH'd in, make sure"
            warn "  you used 'ssh -L 8080:localhost:8080 …' (the bootstrap"
            warn "  picks a random port; replace 8080 with what it prints)."
            warn "  Continuing anyway."
            warn ""
        fi

        exec bash "$REAUTH_SCRIPT"
        ;;

    -h|--help|help)
        sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;

    *)
        warn "Unknown command: $CMD"
        warn "Usage: $(basename "$0") {status|refresh|cron|reauth|help}"
        exit 64
        ;;
esac
