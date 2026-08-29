#!/usr/bin/env bash
# Add a new Claude Max profile to the claude-pool by logging in interactively.
# Usage: bash install/add-claude-profile.sh [--proxy socks5://host:port]
set -euo pipefail

POOL_DIR="$HOME/.reusable-agents/claude-pool"
STATE_JSON="$POOL_DIR/state.json"
PROXY_URL=""
REUSE_SLOT=""
ASSISTED=0
AUTH_CODE_FILE="$HOME/.reusable-agents/claude-pool/.pending-auth-code"

usage() {
  cat <<EOF
Usage: $0 [--proxy <url>] [--reuse-slot <N>]

  --proxy <url>       Record HTTPS/SOCKS proxy for this profile in proxies.conf.
  --assisted          Non-TTY mode for an AI assistant driving the login:
                      prints + opens the OAuth URL in the operator's browser,
                      then waits for the operator's code to be written to
                      ~/.reusable-agents/claude-pool/.pending-auth-code.
  --reuse-slot <N>    Wipe the auth state of profile-<N> and log in a new
                      account into that slot (used when a duplicate account
                      is occupying the slot — frees pool capacity without
                      bumping the profile count).

With no flags, allocates the next sequential profile-N slot.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --proxy) PROXY_URL="$2"; shift 2 ;;
    --reuse-slot) REUSE_SLOT="$2"; shift 2 ;;
    --assisted) ASSISTED=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

# ── Pick profile slot ────────────────────────────────────────────────────────
if [[ -n "$REUSE_SLOT" ]]; then
  if ! [[ "$REUSE_SLOT" =~ ^[0-9]+$ ]]; then
    echo "  ERROR: --reuse-slot must be an integer."
    exit 1
  fi
  PROFILE_ID="profile-$REUSE_SLOT"
  PROFILE_DIR="$POOL_DIR/$PROFILE_ID"
  if [[ ! -d "$PROFILE_DIR" ]]; then
    echo "  ERROR: $PROFILE_DIR does not exist; cannot reuse."
    exit 1
  fi
  echo ""
  echo "  Reusing slot $PROFILE_ID"
  existing_email=$(python3 -c "
import json
try:
    d = json.load(open('$PROFILE_DIR/.claude.json'))
    print(d.get('oauthAccount',{}).get('emailAddress',''))
except Exception:
    print('')
" 2>/dev/null)
  if [[ -n "$existing_email" ]]; then
    echo "  Current account: $existing_email"
    read -r -p "  Wipe its auth and log in a new account? [y/N] " confirm
    [[ "$confirm" != "y" && "$confirm" != "Y" ]] && { echo "  Aborted."; exit 1; }
  fi
  # Backup before wiping
  BACKUP="/tmp/${PROFILE_ID}-backup-$(date +%s).tar.gz"
  tar -czf "$BACKUP" -C "$POOL_DIR" "$PROFILE_ID" 2>/dev/null && \
    echo "  Backup: $BACKUP ($(du -h "$BACKUP" | cut -f1))"
  # Wipe auth-bearing files; keep symlinks
  rm -f "$PROFILE_DIR/.claude.json"
  rm -f "$PROFILE_DIR/.claude/.credentials.json"
  rm -rf "$PROFILE_DIR/.claude/backups"
  rm -rf "$PROFILE_DIR/.claude/sessions"
  rm -rf "$PROFILE_DIR/.claude/projects"
  rm -f  "$PROFILE_DIR/.claude/history.jsonl"
  echo "  ✓ cleared auth state"
else
  # Find next profile number
  next_n=1
  while [[ -d "$POOL_DIR/profile-$next_n" ]]; do
    (( next_n++ ))
  done
  PROFILE_ID="profile-$next_n"
  PROFILE_DIR="$POOL_DIR/$PROFILE_ID"
fi

echo ""
echo "  Target: $PROFILE_ID → $PROFILE_DIR"
echo ""

# ── Scaffold profile dir ─────────────────────────────────────────────────────
mkdir -p "$PROFILE_DIR"

# Symlink shared dotfiles so claude inherits git identity, npm config, etc.
for dotfile in .bashrc .gitconfig .npmrc; do
  target="$HOME/$dotfile"
  link="$PROFILE_DIR/$dotfile"
  if [[ -e "$target" ]] && [[ ! -e "$link" ]]; then
    ln -s "$target" "$link"
  fi
done

# ── Optionally record proxy ──────────────────────────────────────────────────
PROXIES_CONF="$POOL_DIR/proxies.conf"
if [[ -n "$PROXY_URL" ]]; then
  echo "$PROFILE_ID=$PROXY_URL" >> "$PROXIES_CONF"
  echo "  Proxy recorded in proxies.conf: $PROXY_URL"
  echo ""
fi

# ── Run the login interactively ─────────────────────────────────────────────
# MUST be a real terminal. `/login` is the IN-SESSION slash command and only
# works inside an interactive Claude session — invoked without a TTY (e.g. from
# an agent shell, CI, or Claude Code's `!` prefix) it prints
#   "/login isn't available in this environment."
# and exits 0, leaving the profile silently unauthenticated. Fail loudly
# instead so the operator knows to open a normal terminal.
if [[ "$ASSISTED" != "1" ]] && [[ ! -t 0 || ! -t 1 ]]; then
  echo "  ERROR: no TTY. This login is interactive + browser-based." >&2
  echo "         Open a REAL terminal (Windows Terminal -> Ubuntu, or \`wsl\`) and run:" >&2
  echo "           cd $(cd "$(dirname "$0")/.." && pwd) && bash install/add-claude-profile.sh" >&2
  echo "         Running it through an agent/`!` shell cannot work." >&2
  exit 3
fi

echo "  ┌─────────────────────────────────────────────────────────────────┐"
echo "  │  Log in with your Claude Max account in the browser that opens.  │"
echo "  │  After authentication completes, return here.                    │"
echo "  └─────────────────────────────────────────────────────────────────┘"
echo ""

# Prefer the `auth login` SUBCOMMAND (CLI-level, works from a shell); fall back
# to the legacy `/login` slash form for older CLI builds that lack it.
if claude auth --help >/dev/null 2>&1; then
  LOGIN_ARGS=(auth login)
else
  LOGIN_ARGS=(/login)
fi

if [[ "$ASSISTED" == "1" ]]; then
  # Assisted mode. `claude auth login` prints the OAuth URL, then blocks on
  # stdin waiting for the code the operator gets from the browser. So: drive
  # stdin from a FIFO, scrape the URL out of the output, open it on the
  # operator's display, and feed the code back once they hand it over. This is
  # what lets an assistant do everything except the part only a human can do.
  LOGIN_LOG="$PROFILE_DIR/.assisted-login.log"
  FIFO="$(mktemp -u /tmp/claude-auth-fifo.XXXXXX)"
  mkfifo "$FIFO"
  rm -f "$AUTH_CODE_FILE"

  ( exec 3> "$FIFO"
    while [[ ! -s "$AUTH_CODE_FILE" ]]; do sleep 2; done
    tr -d '\r\n' < "$AUTH_CODE_FILE" >&3
    printf '\n' >&3
    exec 3>&- ) &
  FEEDER=$!

  if [[ -n "$PROXY_URL" ]]; then
    HTTPS_PROXY="$PROXY_URL" HTTP_PROXY="$PROXY_URL" HOME="$PROFILE_DIR" \
      BROWSER=/bin/true claude "${LOGIN_ARGS[@]}" --claudeai < "$FIFO" > "$LOGIN_LOG" 2>&1 &
  else
    HOME="$PROFILE_DIR" BROWSER=/bin/true \
      claude "${LOGIN_ARGS[@]}" --claudeai < "$FIFO" > "$LOGIN_LOG" 2>&1 &
  fi
  LOGIN_PID=$!

  AUTH_URL=""
  for _ in $(seq 1 30); do
    AUTH_URL=$(grep -ohE 'https://claude\.com/[^ '"'"'"]+' "$LOGIN_LOG" 2>/dev/null | head -1 || true)
    [[ -n "$AUTH_URL" ]] && break
    sleep 1
  done
  if [[ -z "$AUTH_URL" ]]; then
    echo "  ERROR: no OAuth URL appeared within 30s. Log: $LOGIN_LOG" >&2
    kill $LOGIN_PID $FEEDER 2>/dev/null || true
    rm -f "$FIFO"
    exit 4
  fi

  echo "  Opening this URL in your browser:"
  echo "    $AUTH_URL"
  ( setsid xdg-open "$AUTH_URL" >/dev/null 2>&1 & ) || true
  echo ""
  echo "  Sign in, then paste the code back to the assistant."
  echo "  (The code is written to $AUTH_CODE_FILE to complete the login.)"

  # Wait up to 10 minutes for the operator to authenticate.
  for _ in $(seq 1 300); do
    kill -0 $LOGIN_PID 2>/dev/null || break
    sleep 2
  done
  wait $LOGIN_PID 2>/dev/null || true
  kill $FEEDER 2>/dev/null || true
  rm -f "$FIFO" "$AUTH_CODE_FILE"
  tail -5 "$LOGIN_LOG" 2>/dev/null | sed 's/^/  /'
elif [[ -n "$PROXY_URL" ]]; then
  HTTPS_PROXY="$PROXY_URL" HTTP_PROXY="$PROXY_URL" HOME="$PROFILE_DIR" claude "${LOGIN_ARGS[@]}"
else
  HOME="$PROFILE_DIR" claude "${LOGIN_ARGS[@]}"
fi

# ── Wait for credentials.json to land (pool's auth check looks for it) ─────
# `claude /login` may exit before the OAuth blob is fully flushed to disk;
# the pool's `_is_authenticated()` checks `.claude/.credentials.json` and
# will flip `authenticated: false` if it races.
CREDS_FILE="$PROFILE_DIR/.claude/.credentials.json"
CLAUDE_JSON="$PROFILE_DIR/.claude.json"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [[ -f "$CREDS_FILE" && -f "$CLAUDE_JSON" ]] && break
  sleep 1
done

if [[ ! -f "$CLAUDE_JSON" ]]; then
  echo ""
  echo "  ERROR: $CLAUDE_JSON not found — login may not have completed."
  exit 1
fi
if [[ ! -f "$CREDS_FILE" ]]; then
  echo ""
  echo "  ERROR: $CREDS_FILE not found — pool will mark profile unauthenticated."
  exit 1
fi

EMAIL=$(python3 -c "
import json, sys
try:
    d = json.load(open('$CLAUDE_JSON'))
    print(d.get('oauthAccount', {}).get('emailAddress', ''))
except Exception as e:
    print('', file=sys.stderr)
" 2>/dev/null)

ORG_NAME=$(python3 -c "
import json, sys
try:
    d = json.load(open('$CLAUDE_JSON'))
    print(d.get('oauthAccount', {}).get('organizationName', ''))
except Exception:
    print('')
" 2>/dev/null)

DISCOVERED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo ""
echo "  Login successful!"
[[ -n "$EMAIL" ]] && echo "  Account : $EMAIL"
[[ -n "$ORG_NAME" ]] && echo "  Org     : $ORG_NAME"
echo "  Profile : $PROFILE_ID"
echo ""

# ── Duplicate-account guard ─────────────────────────────────────────────────
# A profile backed by an account already in another slot = zero capacity
# gain. Warn loudly; let the operator decide whether to keep or wipe.
if [[ -n "$EMAIL" ]]; then
  DUPES=$(python3 -c "
import json, os, glob
me = '$PROFILE_DIR'
target_email = '$EMAIL'
dupes = []
for cj in sorted(glob.glob(os.path.expanduser('~/.reusable-agents/claude-pool/profile-*/.claude.json'))):
    profile_dir = os.path.dirname(cj)
    if profile_dir == me:
        continue
    try:
        d = json.load(open(cj))
        email = d.get('oauthAccount',{}).get('emailAddress','')
        if email == target_email:
            dupes.append(os.path.basename(profile_dir))
    except Exception:
        pass
print(','.join(dupes))
" 2>/dev/null)
  if [[ -n "$DUPES" ]]; then
    echo "  ⚠️  WARNING: $EMAIL is already logged into: $DUPES"
    echo "      Same account = same weekly quota = NO capacity gain."
    echo "      Consider re-running with: --reuse-slot ${PROFILE_ID#profile-}"
    echo "      and logging in a different Max account."
    echo ""
  fi
fi

# ── Register in state.json ───────────────────────────────────────────────────
python3 - <<PYEOF
import json, os

state_path = "$STATE_JSON"
profile_id = "$PROFILE_ID"
profile_dir = "$PROFILE_DIR"
discovered_at = "$DISCOVERED_AT"
email = "$EMAIL"

try:
    with open(state_path) as f:
        state = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    state = {}

state[profile_id] = {
    "id": profile_id,
    "home": profile_dir,
    "in_use": 0,
    "last_used_at": "",
    "total_uses": 0,
    "label": email,
    "authenticated": True,
    "discovered_at": discovered_at,
    "limit_resets_at": {},
}

# Write atomically
tmp = state_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f, indent=4)
    f.write("\n")
os.replace(tmp, state_path)

print(f"  Registered {profile_id} in state.json")
PYEOF

# ── Print summary of all profiles ────────────────────────────────────────────
echo ""
echo "  Current pool:"
echo "  ┌──────────────┬──────────┬────────────────────────────────────────────┐"
printf  "  │ %-12s │ %-8s │ %-42s │\n" "Profile" "Auth" "Account / Label"
echo "  ├──────────────┼──────────┼────────────────────────────────────────────┤"

python3 - <<PYEOF
import json

state_path = "$STATE_JSON"
try:
    with open(state_path) as f:
        state = json.load(f)
except Exception:
    state = {}

for pid, info in sorted(state.items()):
    if pid.startswith("__"):
        continue
    auth = "yes" if info.get("authenticated") else "NO"
    label = info.get("label", "")
    if not label:
        # Try reading .claude.json for email
        import os
        cj = os.path.join(info.get("home",""), ".claude.json")
        try:
            d = json.load(open(cj))
            label = d.get("oauthAccount",{}).get("emailAddress","")
        except Exception:
            pass
    print(f"  │ {pid:<12} │ {auth:<8} │ {label:<42} │")
PYEOF

echo "  └──────────────┴──────────┴────────────────────────────────────────────┘"
echo ""
echo "  Done. The pool will use $PROFILE_ID automatically on the next agent run."
echo ""
