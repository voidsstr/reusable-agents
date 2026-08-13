#!/usr/bin/env bash
# recover-credentials.sh — rebuild ~/.reusable-agents/secrets.env on a fresh
# fleet host, and tell the operator exactly which secrets a machine CANNOT
# recover on its own.
#
# Written after the 2026-08 host loss: the retired box's ~/.reusable-agents/
# was the ONLY copy of the GSC OAuth client pair, the claude-pool Max logins,
# the MS Graph/IMAP tokens, the eBay keyset and the BrightData key. None of it
# was ever backed up to Azure. This script exists so that never costs a day
# again — it recovers everything that IS recoverable, and turns the rest into
# a short, exact checklist instead of an archaeology project.
#
# Usage:
#   bash install/recover-credentials.sh harvest   # recover + write secrets.env
#   bash install/recover-credentials.sh status    # what's present / missing
#   bash install/recover-credentials.sh backup    # snapshot secrets to Azure Key Vault
#
# Three tiers of secret:
#   TIER 1 — recoverable from Azure (needs `az login` to the fleet subscription)
#   TIER 2 — recoverable from the repos (DSNs, endpoints, non-secret config)
#   TIER 3 — NOT recoverable by a machine: interactive browser OAuth, or a
#            vendor console copy-paste. Printed as an operator checklist.

set -uo pipefail

STATE_DIR="${STATE_DIR:-$HOME/.reusable-agents}"
SECRETS_FILE="$STATE_DIR/secrets.env"
DEV_ROOT="${DEV_ROOT:-$HOME/development}"
REPO_DIR="${REPO_DIR:-$DEV_ROOT/reusable-agents}"

AZ_SUB="${AZ_SUB:-125b8bc9-e8bb-4827-9d2a-f3492b983dcf}"
AZ_RG="${AZ_RG:-nsc-apps}"
AZ_CONTAINERAPP="${AZ_CONTAINERAPP:-agents}"
AZ_STORAGE_ACCOUNT="${AZ_STORAGE_ACCOUNT:-nscagentstorage}"

bold()   { printf "\033[1m%s\033[0m\n" "$*"; }
green()  { printf "  \033[32m✓\033[0m %s\n" "$*"; }
yellow() { printf "  \033[33m!\033[0m %s\n" "$*"; }
red()    { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }
section(){ echo; bold "── $* ──"; }

az_() { az "$@" --subscription "$AZ_SUB" 2>/dev/null; }

# Never echo a secret. Report length + a 4-char prefix so the operator can
# eyeball that the right value landed without it going into a transcript.
fingerprint() {
    local v="${1:-}"
    if [ -z "$v" ]; then echo "ABSENT"; else echo "len=${#v} prefix=${v:0:4}…"; fi
}

declare -A SECRETS   # KEY -> value
declare -a MISSING   # operator checklist

set_secret() { SECRETS["$1"]="$2"; }

# ── TIER 1: Azure ───────────────────────────────────────────────────────────
harvest_azure() {
    section "TIER 1 — Azure (recoverable by machine)"
    if ! az_ account show >/dev/null 2>&1; then
        red "az cannot reach subscription $AZ_SUB"
        MISSING+=("az login  — then re-run: bash install/recover-credentials.sh harvest")
        return
    fi

    local v
    for pair in "azure-storage-connection-string:AZURE_STORAGE_CONNECTION_STRING" \
                "framework-api-token:FRAMEWORK_API_TOKEN" \
                "google-oauth-client-secret:GOOGLE_OAUTH_CLIENT_SECRET" \
                "session-secret:SESSION_SECRET"; do
        local sname="${pair%%:*}" ename="${pair##*:}"
        v=$(az_ containerapp secret show -g "$AZ_RG" -n "$AZ_CONTAINERAPP" --secret-name "$sname" --query value -o tsv)
        if [ -n "$v" ]; then set_secret "$ename" "$v"; green "$ename $(fingerprint "$v")"
        else yellow "$ename not in containerapp secrets"; fi
    done

    v=$(az_ storage account keys list -g "$AZ_RG" -n "$AZ_STORAGE_ACCOUNT" --query "[0].value" -o tsv)
    if [ -n "$v" ]; then
        set_secret "AZURE_STORAGE_KEY" "$v"
        set_secret "AZURE_STORAGE_ACCOUNT" "$AZ_STORAGE_ACCOUNT"
        green "AZURE_STORAGE_KEY $(fingerprint "$v")"
    else
        yellow "AZURE_STORAGE_KEY unavailable"
    fi
}

# ── TIER 2: the repos ───────────────────────────────────────────────────────
# DSNs and endpoints are embedded in git-tracked manifests/run.sh files. That
# is itself a policy violation (CLAUDE.md forbids hardcoded prod credentials)
# — see docs/fleet-host-standup.md § "Credential hygiene backlog" — but while
# they are there, they are a legitimate recovery source.
harvest_repos() {
    section "TIER 2 — repos (recoverable by machine)"
    local out
    out=$(python3 - "$DEV_ROOT" <<'PY'
import glob, json, os, re, sys
root = sys.argv[1]
dsn_re = re.compile(r'postgresql://[^\s"\'<>)]+')
found = {}
patterns = [
    ('AZURE_OPENAI_API_KEY',  re.compile(r'AZURE_OPENAI_API_KEY=["\']?([A-Za-z0-9_\-]{20,})')),
    ('AZURE_OPENAI_ENDPOINT', re.compile(r'AZURE_OPENAI_ENDPOINT=["\']?(https://[^\s"\']+)')),
    ('BRIGHTDATA_API_KEY',    re.compile(r'BRIGHTDATA_API_KEY=["\']?([A-Za-z0-9_\-]{16,})')),
    ('EBAY_CLIENT_ID',        re.compile(r'EBAY_CLIENT_ID=["\']?([A-Za-z0-9_\-]{10,})')),
    ('EBAY_CLIENT_SECRET',    re.compile(r'EBAY_CLIENT_SECRET=["\']?([A-Za-z0-9_\-]{10,})')),
    ('EBAY_DEV_ID',           re.compile(r'EBAY_DEV_ID=["\']?([A-Za-z0-9_\-]{10,})')),
    ('SEARXNG_URL',           re.compile(r'SEARXNG_URL=["\']?(https?://[^\s"\']+)')),
    ('INDEXNOW_KEY',          re.compile(r'INDEXNOW_KEY=["\']?([A-Za-z0-9]{16,})')),
]
globs = ['*/agents/*/manifest.json','*/agents/*/*.sh','*/agents/*/*.yaml',
         '*/agents/*/*.json','*/.env','*/agents/*/.env',
         # The Search Console OAuth client pair survives in the legacy SEO
         # collector even when ~/.reusable-agents/seo/.oauth.json is lost —
         # that pair is what install/reauth-google-oauth.sh needs to run.
         '*/agents/*/refresh-token.py','*/agents/*/lib/collector/refresh-token.py']
gsc_id = re.compile(r'([0-9]{10,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com)')
gsc_secret = re.compile(r'(GOCSPX-[A-Za-z0-9_-]{20,})')
for g in globs:
    for path in glob.glob(os.path.join(root, g)):
        try:
            txt = open(path, encoding='utf-8', errors='ignore').read()
        except Exception:
            continue
        # Only trust a client pair from a file that actually asks for the
        # Search Console scope — other Google clients on the same GCP project
        # (dashboard sign-in, the mobile app) are NOT interchangeable.
        if 'webmasters' in txt:
            mi, ms = gsc_id.search(txt), gsc_secret.search(txt)
            if mi and 'SEO_AGENT_CLIENT_ID' not in found:
                found['SEO_AGENT_CLIENT_ID'] = mi.group(1)
            if ms and 'SEO_AGENT_CLIENT_SECRET' not in found:
                found['SEO_AGENT_CLIENT_SECRET'] = ms.group(1)
        for m in dsn_re.findall(txt):
            if '${' in m or '$' in m.split('@')[0]:
                continue
            db = m.rsplit('/', 1)[-1].split('?')[0]
            if db in ('aisleprompt', 'specpicks'):
                found.setdefault(f'DATABASE_URL_{db.upper()}', m)
        for name, rx in patterns:
            mm = rx.search(txt)
            if mm and name not in found:
                found[name] = mm.group(1)
for k, v in sorted(found.items()):
    print(f"{k}\t{v}")
PY
)
    while IFS=$'\t' read -r k v; do
        [ -z "$k" ] && continue
        set_secret "$k" "$v"
        green "$k $(fingerprint "$v")"
    done <<< "$out"
    [ -z "$out" ] && yellow "nothing recoverable from repos (are they cloned at $DEV_ROOT?)"
}

# ── TIER 2b: host-local facts ───────────────────────────────────────────────
harvest_local() {
    section "TIER 2b — host-local settings"
    set_secret "STORAGE_BACKEND" "azure"
    set_secret "AZURE_STORAGE_CONTAINER" "agents"
    set_secret "FRAMEWORK_API_URL" "http://127.0.0.1:8090"
    green "STORAGE_BACKEND=azure  AZURE_STORAGE_CONTAINER=agents  FRAMEWORK_API_URL=http://127.0.0.1:8090"

    # OLLAMA_HOST followed the old box's LAN address. Prefer a local daemon;
    # a stale LAN IP silently breaks every ollama fallback + the vision models
    # the hero-image curators depend on.
    if curl -sf --max-time 4 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
        set_secret "OLLAMA_HOST" "http://127.0.0.1:11434"
        green "OLLAMA_HOST=http://127.0.0.1:11434 (local daemon answering)"
    else
        yellow "no local ollama — leaving OLLAMA_HOST unset; ollama-backed fallbacks will fail"
        MISSING+=("ollama: start it locally (systemctl start ollama) or set OLLAMA_HOST to a reachable host")
    fi
}

# ── TIER 3: not machine-recoverable ─────────────────────────────────────────
check_interactive() {
    section "TIER 3 — operator-only (interactive / vendor console)"

    if [ -f "$STATE_DIR/seo/.oauth.json" ]; then
        green "GSC OAuth present ($STATE_DIR/seo/.oauth.json)"
    elif [ -n "${SECRETS[SEO_AGENT_CLIENT_ID]:-}" ] && [ -n "${SECRETS[SEO_AGENT_CLIENT_SECRET]:-}" ]; then
        # Client pair recovered — only the refresh_token needs a human, and the
        # reauth script reads the pair straight out of secrets.env.
        yellow "GSC OAuth: client pair RECOVERED, refresh_token still needs one browser consent"
        MISSING+=("GSC refresh token (client_id + secret already recovered into secrets.env — just run this and click through):
      set -a; . $SECRETS_FILE; set +a; bash $REPO_DIR/install/reauth-google-oauth.sh
      Then publish the consent screen to 'In production' in Google Cloud Console so it stops expiring every 7 days.")
    else
        yellow "GSC OAuth ABSENT — both *-seo-opportunity-agent, gsc-coverage-auditor, indexnow GSC submit, site-goals-tracker are blocked"
        MISSING+=("GSC OAuth: get client_id + client_secret from Google Cloud Console (project 'GrocerAI', OAuth 2.0 Client IDs), then run:
      SEO_AGENT_CLIENT_ID=<id> SEO_AGENT_CLIENT_SECRET=<secret> bash $REPO_DIR/install/reauth-google-oauth.sh
      Then publish the consent screen to 'In production' so the refresh token stops expiring every 7 days.")
    fi

    local pool="$STATE_DIR/claude-pool/state.json" n=0
    if [ -f "$pool" ]; then
        n=$(python3 -c "import json;d=json.load(open('$pool'));print(sum(1 for v in d.values() if isinstance(v,dict) and v.get('authenticated')))" 2>/dev/null || echo 0)
    fi
    if [ "$n" -gt 0 ]; then
        green "claude-pool: $n authenticated profile(s)"
    else
        yellow "claude-pool EMPTY — every Opus-only authoring kind (article-author, news-author, news-rewrite, h2h) hard-defers; publish volume stays 0"
        MISSING+=("claude-pool: log in at least one Max account (each is a browser flow):
      python3 -m framework.cli.claude_pool login-help     # prints the exact per-profile commands
      bash $REPO_DIR/install/add-claude-profile.sh        # or add a profile interactively
      Do NOT point the fleet at your personal ~/.claude/.credentials.json.")
    fi

    if command -v msmtp >/dev/null 2>&1 && [ -f "$HOME/.msmtprc" ]; then
        green "msmtp configured (operator email works)"
    else
        yellow "msmtp missing/unconfigured — KTLO escalations and every operator alert fail SILENTLY"
        MISSING+=("email: sudo apt-get install -y msmtp msmtp-mta, then author ~/.msmtprc (mode 0600) with the 'automation' account
      (from: automation@northernsoftwareconsulting.com — never change it; recipients are always mperry@northernsoftwareconsulting.com)")
    fi

    if [ -n "${SECRETS[EBAY_CLIENT_ID]:-}" ]; then
        green "eBay keyset recovered from repos"
    else
        yellow "eBay keyset ABSENT — specpicks-ebay-product-sync-agent cannot ingest retro listings"
        MISSING+=("eBay: developer.ebay.com → Application Keysets → copy Client ID / Client Secret / Dev ID into $SECRETS_FILE
      as EBAY_CLIENT_ID / EBAY_CLIENT_SECRET / EBAY_DEV_ID (also EBAY_ENV=PRODUCTION, EBAY_MARKETPLACE_ID=EBAY_US)")
    fi

    if [ -n "${SECRETS[BRIGHTDATA_API_KEY]:-}" ]; then
        green "BrightData key recovered from repos"
    else
        yellow "BrightData key ABSENT — specpicks-amazon-price-verifier proxy refresh fails (known_excluded, not a blocker)"
        MISSING+=("BrightData (optional): console → API tokens → set BRIGHTDATA_API_KEY in $SECRETS_FILE")
    fi

    if [ -f "$STATE_DIR/responder/.oauth.json" ]; then
        green "responder IMAP/Graph OAuth present"
    else
        yellow "responder OAuth ABSENT — responder-agent (*/2 cron) will fail every 2 minutes"
        MISSING+=("responder mail OAuth: bash $REPO_DIR/install/setup-microsoft-oauth.sh (or setup-imap-oauth.sh)
      Needs the Azure App Registration MS_GRAPH_CLIENT_ID + MS_GRAPH_TENANT_ID.")
    fi
}

# ── write ───────────────────────────────────────────────────────────────────
write_secrets() {
    section "writing $SECRETS_FILE"
    mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"

    # Preserve any keys the operator added by hand that we did not re-derive.
    if [ -f "$SECRETS_FILE" ]; then
        cp -p "$SECRETS_FILE" "$SECRETS_FILE.bak.$(date +%Y%m%d%H%M%S)"
        while IFS='=' read -r k v; do
            [[ "$k" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
            [ -z "${SECRETS[$k]:-}" ] && SECRETS["$k"]="$v"
        done < "$SECRETS_FILE"
        green "merged existing values (backup written)"
    fi

    {
        echo "# reusable-agents fleet host secrets — generated by install/recover-credentials.sh"
        echo "# $(date -u +%Y-%m-%dT%H:%M:%SZ)   host=$(hostname)"
        echo "#"
        echo "# Plain KEY=val (NOT exported). Consumers must use:  set -a; . secrets.env; set +a"
        echo "# Referenced by every generated agent unit as EnvironmentFile=-$SECRETS_FILE"
        echo "# (leading '-' means systemd tolerates absence — units start GREEN and then fail"
        echo "#  at runtime on a missing DSN, so never trust a clean 'systemctl start' as proof.)"
        echo
        for k in $(printf '%s\n' "${!SECRETS[@]}" | sort); do
            local v="${SECRETS[$k]}"
            # MUST single-quote. Consumers source this with `set -a; . secrets.env`,
            # and an Azure connection string is full of ';' — unquoted, the shell
            # treats it as a command separator and the value is TRUNCATED at the
            # first ';'. That surfaces far away as
            #   ValueError: Connection string missing required connection details
            # and looks like a bad secret rather than a quoting bug. systemd's
            # EnvironmentFile= strips the same quotes, so this is safe for both.
            v="${v%\'}"; v="${v#\'}"          # drop pre-existing wrapping quotes
            v="${v%\"}"; v="${v#\"}"
            printf "%s='%s'\n" "$k" "${v//\'/\'\\\'\'}"
        done
    } > "$SECRETS_FILE"
    chmod 600 "$SECRETS_FILE"
    green "wrote $(wc -l < "$SECRETS_FILE") lines, mode 0600"
}

# ── backup to Key Vault ─────────────────────────────────────────────────────
# The whole point: never again have a host be the only copy.
cmd_backup() {
    section "backup secrets.env -> Azure Key Vault"
    local kv="${AZ_KEYVAULT:-nsc-fleet-secrets}"
    if ! az_ keyvault show -n "$kv" >/dev/null 2>&1; then
        yellow "key vault '$kv' does not exist. Create it once (operator):"
        echo "    az keyvault create -g $AZ_RG -n $kv --subscription $AZ_SUB --enable-rbac-authorization false"
        return 1
    fi
    [ -f "$SECRETS_FILE" ] || { red "no $SECRETS_FILE to back up"; return 1; }
    az_ keyvault secret set --vault-name "$kv" --name "fleet-secrets-env" \
        --file "$SECRETS_FILE" --only-show-errors >/dev/null \
        && green "uploaded secrets.env to key vault $kv" \
        || red "upload failed"
    for d in seo responder claude-pool; do
        [ -d "$STATE_DIR/$d" ] || continue
        local tar="/tmp/fleet-$d.tar.gz"
        tar czf "$tar" -C "$STATE_DIR" "$d" 2>/dev/null || continue
        az_ keyvault secret set --vault-name "$kv" --name "fleet-$d-tgz" \
            --value "$(base64 -w0 "$tar")" --only-show-errors >/dev/null \
            && green "backed up $d/ ($(stat -c %s "$tar") bytes)"
        rm -f "$tar"
    done
}

cmd_status() {
    section "credential status"
    if [ -f "$SECRETS_FILE" ]; then
        green "$SECRETS_FILE present ($(grep -c '^[A-Z]' "$SECRETS_FILE") keys, mode $(stat -c %a "$SECRETS_FILE"))"
        # Load what is already stored, else every check below reports ABSENT
        # regardless of reality (status ran the harvest-time branches against an
        # empty SECRETS map and wrongly told the operator to re-fetch the GSC
        # client pair that had already been recovered).
        while IFS='=' read -r k v; do
            [[ "$k" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
            v="${v%\'}"; v="${v#\'}"; v="${v%\"}"; v="${v#\"}"
            SECRETS["$k"]="$v"
        done < "$SECRETS_FILE"
    else
        red "$SECRETS_FILE ABSENT"
    fi
    check_interactive
    print_checklist
}

print_checklist() {
    if [ ${#MISSING[@]} -eq 0 ]; then
        section "nothing left for the operator — all credentials present"
        return
    fi
    section "OPERATOR CHECKLIST (${#MISSING[@]} item(s) a machine cannot do)"
    local i=1
    for m in "${MISSING[@]}"; do
        printf "\n  \033[1m%d.\033[0m %s\n" "$i" "$m"
        i=$((i+1))
    done
    echo
    echo "  Re-check any time with:  bash install/recover-credentials.sh status"
}

main() {
    case "${1:-harvest}" in
        harvest)
            harvest_azure; harvest_repos; harvest_local
            write_secrets
            check_interactive
            print_checklist ;;
        status) cmd_status ;;
        backup) cmd_backup ;;
        *) echo "usage: $0 {harvest|status|backup}" >&2; exit 2 ;;
    esac
}

main "$@"
