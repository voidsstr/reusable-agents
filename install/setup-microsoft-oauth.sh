#!/usr/bin/env bash
# setup-microsoft-oauth.sh — one-time interactive setup for Microsoft Graph
# email sending via the existing `nsc-assistant` Azure App Registration.
#
# What it does:
#   1. Runs oauth-bootstrap.py for the Microsoft provider against the
#      pre-configured app registration (client_id + tenant_id baked in).
#   2. You sign in as automation@northernsoftwareconsulting.com in the
#      browser that opens. Click Allow on the consent screen.
#   3. The bootstrap writes ~/.reusable-agents/responder/.oauth.json with
#      a refresh_token (mode 0600).
#   4. Mints a test access_token to confirm the refresh_token works.
#   5. Sends a real test email through Microsoft Graph /sendMail to
#      mperry@northernsoftwareconsulting.com so you can confirm
#      end-to-end delivery.
#
# Run this on the same machine where you can use a graphical browser.
# It is idempotent: re-running just refreshes the .oauth.json.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — read from environment variables.
# Set these in your .env (or export them before running):
#   MS_GRAPH_CLIENT_ID       Azure App Registration (client) ID
#   MS_GRAPH_TENANT_ID       Azure tenant ID
#   MS_GRAPH_SIGNIN_HINT     Mailbox to sign in as (e.g. automation@your-domain.com)
#   MS_GRAPH_TEST_RECIPIENT  (optional) recipient for the roundtrip test
#                            (defaults to the signin hint)
#
# To create the Azure App Registration: see install/setup-microsoft-oauth.md
# (or follow the inline doc at the top of agents/responder-agent/oauth-bootstrap.py).
# ---------------------------------------------------------------------------
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESPONDER_DIR="${REPO}/agents/responder-agent"
OAUTH_FILE="${OAUTH_FILE:-${HOME}/.reusable-agents/responder/.oauth.json}"

# Auto-source .env if present (so $MS_GRAPH_* are picked up)
if [[ -f "${REPO}/.env" ]] && [[ -z "${MS_GRAPH_CLIENT_ID:-}" ]]; then
    set -a; . "${REPO}/.env"; set +a
fi

CLIENT_ID="${MS_GRAPH_CLIENT_ID:-}"
TENANT_ID="${MS_GRAPH_TENANT_ID:-}"
SIGNIN_HINT="${MS_GRAPH_SIGNIN_HINT:-}"
TEST_RECIPIENT="${MS_GRAPH_TEST_RECIPIENT:-${SIGNIN_HINT}}"

if [[ -z "$CLIENT_ID" || -z "$TENANT_ID" || -z "$SIGNIN_HINT" ]]; then
    cat >&2 <<EOF
ERROR: missing required environment variables.

Set these in your .env (or export before running):

  MS_GRAPH_CLIENT_ID=<azure-app-client-id-guid>
  MS_GRAPH_TENANT_ID=<azure-tenant-id-guid>
  MS_GRAPH_SIGNIN_HINT=<mailbox-to-sign-in-as>
  MS_GRAPH_TEST_RECIPIENT=<optional-test-recipient>

To create the Azure App Registration first time, see
install/setup-microsoft-oauth.md.
EOF
    exit 64
fi

# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*" >&2; }
hr()    { printf -- "──────────────────────────────────────────────────────────\n"; }

bold "Microsoft Graph OAuth setup — reusable-agents framework"
hr
echo "  Client ID:        $CLIENT_ID"
echo "  Tenant ID:        $TENANT_ID"
echo "  Sign in as:       $SIGNIN_HINT"
echo "  Test recipient:   $TEST_RECIPIENT"
echo "  Token saved to:   $OAUTH_FILE"
hr
echo

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if [[ ! -f "${RESPONDER_DIR}/oauth-bootstrap.py" ]]; then
  red "ERROR: cannot find oauth-bootstrap.py at ${RESPONDER_DIR}/"
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  red "ERROR: python3 not on PATH"
  exit 1
fi

mkdir -p "$(dirname "$OAUTH_FILE")"

# ---------------------------------------------------------------------------
# 1. Run the bootstrap.
# ---------------------------------------------------------------------------
bold "[1/3] Running OAuth bootstrap (device-code flow)"
echo "  This flow doesn't need a localhost callback — it works over SSH"
echo "  and from headless terminals. The script will print a short code"
echo "  + a URL. Open the URL on ANY device with a browser (your phone,"
echo "  laptop, etc), enter the code, sign in as $SIGNIN_HINT, and click"
echo "  Allow. The script polls and returns once you're done."
echo
cd "$RESPONDER_DIR"
python3 oauth-bootstrap.py \
    --provider microsoft \
    --client-id "$CLIENT_ID" \
    --tenant   "$TENANT_ID" \
    --device-code

if [[ ! -s "$OAUTH_FILE" ]]; then
  red
  red "ERROR: bootstrap completed but $OAUTH_FILE is empty or missing."
  red "Check the script output above for the actual error."
  exit 1
fi
chmod 600 "$OAUTH_FILE"
green "  ✓ Refresh token saved to $OAUTH_FILE"
echo

# ---------------------------------------------------------------------------
# 2. Verify the refresh_token mints a working access_token.
# ---------------------------------------------------------------------------
bold "[2/3] Minting a test access_token via mint-token.py"
TEST_OUT=$(python3 - <<'PY'
import sys, importlib.util, pathlib
p = pathlib.Path("mint-token.py")
spec = importlib.util.spec_from_file_location("mint_token", str(p))
mt = importlib.util.module_from_spec(spec); spec.loader.exec_module(mt)
oauth = pathlib.Path.home() / ".reusable-agents/responder/.oauth.json"
try:
    token, user, prov = mt.mint_access_token(
        oauth,
        scope_override="offline_access https://graph.microsoft.com/Mail.Send",
    )
except Exception as e:
    print(f"FAIL: {e}", file=sys.stderr)
    sys.exit(1)
print(f"OK len={len(token)} user={user!r} provider={prov!r}")
PY
) || { red "  ✗ Token mint failed:"; echo "$TEST_OUT" >&2; exit 1; }
green "  ✓ ${TEST_OUT}"
echo

# ---------------------------------------------------------------------------
# 3. Send a real test email via Graph /sendMail.
# ---------------------------------------------------------------------------
bold "[3/3] Sending a roundtrip test email"
echo "  → from: $SIGNIN_HINT"
echo "  → to:   $TEST_RECIPIENT"
echo
SEND_OUT=$(python3 - <<PY
import sys, importlib.util, pathlib, json, urllib.request
p = pathlib.Path("mint-token.py")
spec = importlib.util.spec_from_file_location("mint_token", str(p))
mt = importlib.util.module_from_spec(spec); spec.loader.exec_module(mt)
oauth = pathlib.Path.home() / ".reusable-agents/responder/.oauth.json"
token, user, _ = mt.mint_access_token(
    oauth, scope_override="offline_access https://graph.microsoft.com/Mail.Send")
sender = "$SIGNIN_HINT"
to     = "$TEST_RECIPIENT"
body = {
    "message": {
        "subject": "[reusable-agents] Microsoft Graph OAuth test — delivery confirmed",
        "body": {"contentType": "HTML",
                 "content": "<p>If you can read this, the agent fleet's Graph send path is working.</p>"
                            "<p>Refresh-token flow via the <code>nsc-assistant</code> Azure App Registration. "
                            "msmtp basic-auth no longer required.</p>"},
        "toRecipients": [{"emailAddress": {"address": to}}],
        "from":   {"emailAddress": {"address": sender}},
        "sender": {"emailAddress": {"address": sender}},
    },
    "saveToSentItems": "true",
}
req = urllib.request.Request(
    f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
    data=json.dumps(body).encode(), method="POST",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
)
try:
    resp = urllib.request.urlopen(req, timeout=30)
    print(f"HTTP {resp.status} — sent.")
except urllib.error.HTTPError as e:
    msg = e.read().decode("utf-8", "replace")[:600]
    print(f"FAIL: HTTP {e.code} {e.reason}\n{msg}", file=sys.stderr)
    sys.exit(1)
PY
) || { red "  ✗ Test email failed:"; echo "$SEND_OUT" >&2;
       red "    The OAuth file is still valid; agents will use it. The"
       red "    failure here is at the /sendMail call. Common causes:"
       red "      • Mail.Send not granted to user (check admin consent)"
       red "      • The signed-in user can't send AS automation@... (try /me/sendMail instead of /users/.../sendMail)"
       exit 1; }
green "  ✓ ${SEND_OUT}"
echo
hr
green "All three steps passed. Email sending should work fleet-wide now."
echo
echo "Next: tell the assistant the bootstrap finished — it'll re-trigger"
echo "the comp-research and app-store-opportunity agents to confirm"
echo "delivery end-to-end."
