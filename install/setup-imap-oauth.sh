#!/usr/bin/env bash
# setup-imap-oauth.sh — bootstrap the OUTLOOK-scoped OAuth token the
# responder needs for IMAP polling.
#
# Why a second token? Microsoft AAD requires single-resource scopes
# per token request. The first OAuth bootstrap (setup-microsoft-oauth.sh)
# requested Microsoft Graph scopes (Mail.Send) — that token can SEND
# email but can't read the mailbox via IMAP. The responder polls the
# Outlook IMAP server, which needs the legacy outlook.office.com
# scope set. So we run a second bootstrap with --scope-set outlook
# and write the resulting refresh-token to a SEPARATE oauth file.
#
# Same Azure App Registration; different scopes; different output file.
#
# Run this on the same machine where you can use a graphical browser.
# Idempotent — re-running just refreshes the .imap-oauth.json.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESPONDER_DIR="${REPO}/agents/responder-agent"
RESPONDER_CONFIG="${RESPONDER_CONFIG:-$HOME/.reusable-agents/responder/config.yaml}"
OAUTH_FILE="${IMAP_OAUTH_FILE:-${HOME}/.reusable-agents/responder/.imap-oauth.json}"

# Auto-source .env (so MS_GRAPH_* are picked up). Same env vars as
# setup-microsoft-oauth.sh — if you ran that, you don't need to set
# anything else.
if [[ -f "${REPO}/.env" ]] && [[ -z "${MS_GRAPH_CLIENT_ID:-}" ]]; then
    set -a; . "${REPO}/.env"; set +a
fi

CLIENT_ID="${MS_GRAPH_CLIENT_ID:-}"
TENANT_ID="${MS_GRAPH_TENANT_ID:-}"
SIGNIN_HINT="${MS_GRAPH_SIGNIN_HINT:-}"

# Fallback: pull client_id + tenant from the existing Graph oauth file
# (you already ran setup-microsoft-oauth.sh, which stored them).
EXISTING_OAUTH="${HOME}/.reusable-agents/responder/.oauth.json"
if [[ -z "$CLIENT_ID" && -s "$EXISTING_OAUTH" ]]; then
    CLIENT_ID="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('client_id',''))" "$EXISTING_OAUTH" 2>/dev/null || echo "")"
fi
if [[ -z "$TENANT_ID" && -s "$EXISTING_OAUTH" ]]; then
    TENANT_ID="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('tenant',''))" "$EXISTING_OAUTH" 2>/dev/null || echo "")"
fi
# SIGNIN_HINT — prompt if still missing
if [[ -z "$SIGNIN_HINT" ]]; then
    read -r -p "Mailbox to sign in as for IMAP polling (e.g. automation@your-domain.com): " SIGNIN_HINT
fi

if [[ -z "$CLIENT_ID" || -z "$TENANT_ID" || -z "$SIGNIN_HINT" ]]; then
    cat >&2 <<EOF
ERROR: missing required environment variables.

Set in your .env (or export before running) — same vars as
setup-microsoft-oauth.sh:

  MS_GRAPH_CLIENT_ID=<azure-app-client-id-guid>
  MS_GRAPH_TENANT_ID=<azure-tenant-id-guid>
  MS_GRAPH_SIGNIN_HINT=<mailbox-username-to-poll>

The Azure App Registration MUST have these DELEGATED permissions
granted with admin consent:
  • IMAP.AccessAsUser.All  (under outlook.office.com)
  • SMTP.Send              (under outlook.office.com — optional,
                            for fallback msmtp + XOAUTH2)
  • offline_access

If you only granted the Microsoft Graph Mail.Send scopes earlier,
go back to the Azure portal, add the IMAP.AccessAsUser.All scope
to the same App Registration, click "Grant admin consent", then
run this script.
EOF
    exit 64
fi

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*" >&2; }
hr()    { printf -- "──────────────────────────────────────────────────────────\n"; }

bold "IMAP OAuth setup (outlook scope) — reusable-agents responder"
hr
echo "  Client ID:        $CLIENT_ID"
echo "  Tenant ID:        $TENANT_ID"
echo "  Sign in as:       $SIGNIN_HINT"
echo "  IMAP token saved: $OAUTH_FILE"
echo "  Existing Graph token (sending) is NOT touched."
hr
echo

bold "[1/3] Running OAuth bootstrap with --scope-set=outlook"
echo "  Device-code flow: open the URL printed below on any browser,"
echo "  enter the short code, sign in as $SIGNIN_HINT, click Allow."
echo
mkdir -p "$(dirname "$OAUTH_FILE")"
cd "$RESPONDER_DIR"
python3 oauth-bootstrap.py \
    --provider microsoft \
    --client-id "$CLIENT_ID" \
    --tenant   "$TENANT_ID" \
    --device-code \
    --scope-set outlook \
    --out      "$OAUTH_FILE"

if [[ ! -s "$OAUTH_FILE" ]]; then
    red "ERROR: bootstrap completed but $OAUTH_FILE is empty or missing."
    exit 1
fi
chmod 600 "$OAUTH_FILE"
green "  ✓ IMAP refresh-token saved to $OAUTH_FILE"
echo

# ---------------------------------------------------------------------------
# 2. Point the responder config at the new token (idempotent)
# ---------------------------------------------------------------------------
bold "[2/3] Updating responder config to use the IMAP-scoped token"
if [[ -f "$RESPONDER_CONFIG" ]]; then
    # Use python to safely rewrite the YAML rather than sed (which would
    # mangle multi-line blocks).
    python3 - "$RESPONDER_CONFIG" "$OAUTH_FILE" <<'PY'
import sys, re, pathlib
cfg_path = pathlib.Path(sys.argv[1])
new_oauth = sys.argv[2].replace(str(pathlib.Path.home()), "~")
text = cfg_path.read_text()
# imap.oauth_file: ~/.reusable-agents/responder/.oauth.json   ← old
# imap.oauth_file: ~/.reusable-agents/responder/.imap-oauth.json   ← new
new = re.sub(
    r"^(\s*oauth_file:\s*)~/\.reusable-agents/responder/\.oauth\.json\s*$",
    rf"\1{new_oauth}",
    text, flags=re.MULTILINE,
)
if new != text:
    cfg_path.write_text(new)
    print(f"  ✓ Updated imap.oauth_file → {new_oauth}")
else:
    print(f"  (config already pointing at {new_oauth} — no change)")
PY
else
    echo "  (warning: $RESPONDER_CONFIG not found; you'll need to set"
    echo "   imap.oauth_file: $OAUTH_FILE manually)"
fi
echo

# ---------------------------------------------------------------------------
# 3. Smoke test — mint a fresh access token + try one IMAP login
# ---------------------------------------------------------------------------
bold "[3/3] Smoke-testing IMAP login with the new token"
python3 - "$OAUTH_FILE" "$SIGNIN_HINT" <<'PY'
import sys, importlib.util, pathlib
oauth_file = pathlib.Path(sys.argv[1])
username   = sys.argv[2]

mt_path = pathlib.Path(__file__).resolve().parent.parent / "agents" / "responder-agent" / "mint-token.py"
spec = importlib.util.spec_from_file_location("mint_token", str(mt_path))
mt = importlib.util.module_from_spec(spec); spec.loader.exec_module(mt)

token, _user, _prov = mt.mint_access_token(
    oauth_file,
    scope_override="offline_access https://outlook.office.com/IMAP.AccessAsUser.All",
)
print(f"  ✓ Minted IMAP access_token (len={len(token)})")

import imaplib
M = imaplib.IMAP4_SSL("outlook.office365.com", 993)
auth_string = f"user={username}\x01auth=Bearer {token}\x01\x01"
try:
    M.authenticate("XOAUTH2", lambda _x: auth_string.encode())
    M.select("INBOX")
    typ, data = M.search(None, "ALL")
    n = len(data[0].split()) if data and data[0] else 0
    print(f"  ✓ IMAP authenticated; INBOX has {n} message(s)")
    M.logout()
except Exception as e:
    print(f"  ✗ IMAP login failed: {e}", file=sys.stderr)
    sys.exit(1)
PY

hr
green "All steps passed. The responder will pick up your reply on its"
green "next 2-min poll. Watch /tmp/reusable-agents-logs/responder-agent-*.log"
green "for messages>0 and a dispatch confirmation."
echo
echo "Tell the assistant the bootstrap is done — it'll resume the"
echo "end-to-end monitoring."
