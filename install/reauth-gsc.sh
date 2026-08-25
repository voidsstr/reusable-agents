#!/usr/bin/env bash
# reauth-gsc.sh — re-auth Google Search Console with the READ-WRITE scope so
# sitemaps are submittable, then verify it and back the token up to Key Vault.
#
#   bash install/reauth-gsc.sh
#
# WHY THIS EXISTS
#   install/reauth-google-oauth.sh documents "webmasters (GSC — read +
#   Sitemaps.submit)" in its header, but refresh-token.py requested
#   webmasters.READONLY. Readonly can read coverage + search analytics but
#   CANNOT call Sitemaps.submit, so IndexNow's sitemap pings 403 while every
#   other GSC call looks healthy. Nothing failed loudly; submission just never
#   worked. This wrapper forces the read-write scope and then VERIFIES what
#   Google actually granted, instead of assuming the request was honoured.
#
#   MUST run where a browser can open and reach a localhost callback — i.e. at
#   this box's desktop, not over an SSH-only session.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OAUTH_FILE="${OAUTH_FILE:-$HOME/.reusable-agents/seo/.oauth.json}"
RW_SCOPE="https://www.googleapis.com/auth/webmasters"
c(){ printf '\033[36m[reauth-gsc]\033[0m %s\n' "$*"; }
ok(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad(){ printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }

[ -t 0 ] || { bad "no TTY — this is an interactive browser flow. Run it in a real terminal."; exit 1; }

c "requesting READ-WRITE scope ($RW_SCOPE)"
# GSC_READONLY unset => refresh-token.py asks for read-write (see its SCOPES).
unset GSC_READONLY || true

# The consent flow itself (access_type=offline + prompt=consent => new refresh token)
bash "$REPO_ROOT/install/reauth-google-oauth.sh"

# ── Verify what Google actually GRANTED, not what we asked for ──────────────
c "verifying granted scopes"
python3 - "$OAUTH_FILE" "$RW_SCOPE" <<'PY'
import json, sys, urllib.parse, urllib.request
oauth_file, rw = sys.argv[1], sys.argv[2]
d = json.load(open(oauth_file))
body = urllib.parse.urlencode({
    "client_id": d["client_id"], "client_secret": d["client_secret"],
    "refresh_token": d["refresh_token"], "grant_type": "refresh_token"}).encode()
try:
    r = urllib.request.urlopen(urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body), timeout=30)
except urllib.error.HTTPError as e:
    print(f"  \033[31m✗\033[0m refresh FAILED HTTP {e.code}: {e.read().decode()[:200]}")
    raise SystemExit(3)
tok = json.load(r)
granted = (tok.get("scope") or "").split()
for s in sorted(granted):
    print(f"    - {s}")
if rw not in granted:
    print(f"  \033[31m✗\033[0m read-write scope NOT granted — sitemaps still cannot be submitted.")
    print( "     On the consent screen you must approve the Search Console permission that")
    print( "     says 'View and manage', not 'View'. Re-run this script and accept all boxes.")
    raise SystemExit(4)
print(f"  \033[32m✓\033[0m read-write GSC scope granted — Sitemaps.submit is available")

# Prove it against the live API: list sitemaps for each owned property.
at = tok["access_token"]
def api(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(
        u, headers={"Authorization": "Bearer " + at}), timeout=30))
sites = api("https://searchconsole.googleapis.com/webmasters/v3/sites")
for s in sites.get("siteEntry", []):
    if s.get("permissionLevel") != "siteOwner":
        continue
    u = urllib.parse.quote(s["siteUrl"], safe="")
    try:
        sm = api(f"https://searchconsole.googleapis.com/webmasters/v3/sites/{u}/sitemaps")
        n = len(sm.get("sitemap", []))
        print(f"  \033[32m✓\033[0m {s['siteUrl']}: {n} sitemap(s) currently registered")
    except Exception as e:
        print(f"  ! {s['siteUrl']}: sitemap list failed ({e})")
PY

# ── Persist to Key Vault so a host rebuild does not need another browser ────
c "backing up to Azure Key Vault"
if command -v az >/dev/null && bash "$REPO_ROOT/install/recover-credentials.sh" backup 2>&1 | grep -q "backed up seo"; then
    ok "seo/.oauth.json stored as fleet-seo-tgz"
else
    bad "Key Vault backup did not run — do it manually: bash install/recover-credentials.sh backup"
fi

c "done. Re-run the SEO agents to pick it up:"
echo "    systemctl --user start agent-aisleprompt-seo-opportunity-agent.service"
echo "    systemctl --user start agent-specpicks-seo-opportunity-agent.service"
echo
c "REMINDER: publish the OAuth consent screen to 'In production' in the GCP"
c "console, or Google revokes this refresh token again in 7 days."
