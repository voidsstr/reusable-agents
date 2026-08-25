---
name: refresh-gsc-token
description: >
  Re-mint the shared Google Search Console + GA4 OAuth refresh token when the
  SEO collector starts failing, and get it back into Key Vault. Invoke when the
  operator or a keep-the-lights-on tick reports any of: "collector exited rc=1",
  "token mint failed", "invalid_grant", "Token has been expired or revoked",
  "gsc-coverage-auditor is failing", "seo-opportunity-agent won't run",
  "sitemap pings are 403ing / ok=8/15", "IndexNow can't submit sitemaps", or
  "refresh the GSC token". Also covers the read-only vs read-write scope choice
  and the 7-day expiry that causes this to recur.
---

# Refresh the GSC / GA4 OAuth token

One Google refresh token, shared by both sites, lives at
**`~/.reusable-agents/seo/.oauth.json`** (0600). Everything Search-Console
touches depends on it: both `*-seo-opportunity-agent`s, both
`*-gsc-coverage-auditor`s, and the IndexNow submitters' sitemap pings.

---

## 1. Confirm it is actually the token (30 seconds, no side effects)

Do this first — several unrelated failures look identical in the agent log.

```bash
python3 - <<'PY'
import json,urllib.request,urllib.parse
d=json.load(open('/home/voidsstr/.reusable-agents/seo/.oauth.json'))
b=urllib.parse.urlencode({"client_id":d["client_id"],"client_secret":d["client_secret"],
  "refresh_token":d["refresh_token"],"grant_type":"refresh_token"}).encode()
try:
    t=json.load(urllib.request.urlopen(urllib.request.Request(
        "https://oauth2.googleapis.com/token",data=b),timeout=30))
    print("ALIVE. granted scopes:"); [print("  -",s) for s in sorted(t["scope"].split())]
except urllib.error.HTTPError as e:
    print("DEAD:", e.code, e.read().decode()[:200])
PY
```

- `invalid_grant / Token has been expired or revoked` → the grant is gone. Go to §3.
- `ALIVE` but the scope list lacks `https://www.googleapis.com/auth/webmasters`
  (only `webmasters.readonly`) → reads work, **sitemap submission does not**. Go to §3.
- `ALIVE` with read-write → the token is fine; the failure is something else
  (check the agent log for the real error before touching anything).

**Do not go hunting for a good copy on another host.** Verified 2026-08-25:
whitebeast's `.oauth.json` was byte-identical to the vault's (same SHA-256 on
client_id, client_secret AND refresh_token). There is exactly one token; when
it is revoked, no copy of it anywhere works. Only a browser consent re-mints it.

---

## 2. What a revoked token looks like from the agents

| symptom | where |
|---|---|
| `[collector] token mint failed rc=2: Token refresh failed: HTTP Error 400` | `agent-<site>-seo-opportunity-agent.log` |
| `failure (0%) collector exited rc=1` — within ~15s of start | same |
| coverage auditor produces 0 rows over many runs | `*-gsc-coverage-auditor` |
| `sitemap pings ok=8/15` (not 15/15) | `*-indexnow-submitter` — this one means **readonly scope**, not a dead token |

⚠️ **Do not add a blanket `known_excluded` for `collector exited rc=1`.**
specpicks carried one reading *"GSC OAuth refresh window; auto-recovers"* — it
does not auto-recover, and that exclusion hid a 12-day outage. It only surfaced
because aisleprompt had no equivalent rule. Narrowed in specpicks commit
`da133e3`; keep it narrow.

---

## 3. Re-mint it

```bash
cd ~/development/reusable-agents && bash install/reauth-gsc.sh
```

**Must run at the machine's own desktop** — it opens a browser and needs a
localhost callback. SSH-only sessions fail; the script refuses without a TTY
rather than half-completing. It cannot be run by an agent.

On the consent screen approve **every** box. For Search Console pick the
permission that says **"View and manage"**, not "View" — "View" is
`webmasters.readonly` and silently costs you sitemap submission.

`install/reauth-gsc.sh` wraps `reauth-google-oauth.sh` and adds what that one
lacked: it reads back the scopes Google **granted** (a request is not a grant —
the 2026-08-25 re-auth asked for read-write and got readonly because
`refresh-token.py` requested `webmasters.readonly`, fixed in `b2d8d53`), fails
loudly if read-write is missing, proves the permission against the live API, and
backs the result up to Key Vault.

Scope control: read-write is the default; `GSC_READONLY=1` requests the narrow
scope.

---

## 4. Verify — by output, not by "the script said OK"

```bash
# 1. scope actually granted (must list .../auth/webmasters, NOT just .readonly)
#    → re-run the §1 snippet

# 2. the write path really works: re-submitting an already-registered sitemap
#    is idempotent and is exactly what IndexNow does. HTTP 204 = accepted.
#    lastSubmitted should jump to now.

# 3. the agents that were failing
systemctl --user start agent-aisleprompt-seo-opportunity-agent.service
systemctl --user start agent-specpicks-seo-opportunity-agent.service
grep -E '\[collector\]' /tmp/reusable-agents-logs/agent-aisleprompt-seo-opportunity-agent.log | tail -5
#    healthy looks like: ✓ ga4-*.json rows, ✓ site-signals.json,
#    ✓ sitemap-urls.json: N urls  — NOT "token mint failed"

# 4. the downstream symptom
systemctl --user start agent-aisleprompt-indexnow-submitter.service
#    healthy: "sitemap pings ok=15/15"   broken/readonly: "ok=8/15"
```

---

## 5. Back it up — it exists in exactly one place otherwise

`install/reauth-gsc.sh` does this automatically. If it reported the backup did
not run:

```bash
bash install/recover-credentials.sh backup    # → fleet-seo-tgz in nsc-secrets-kv
```

Verify the round trip rather than trusting the upload — pull `fleet-seo-tgz`
back down and compare the `refresh_token` SHA-256 against the live file. A
backup that captured the wrong bytes is worse than none: the vault's
claude-pool credentials once round-tripped "successfully" while holding empty
token strings.

---

## 6. The durable fix — otherwise this recurs every 7 days

**Google revokes refresh tokens after 7 days while the OAuth consent screen is
in *Testing*.** That is what killed the token minted 2026-08-13 by 2026-08-25.
Re-authing buys another week and nothing more.

**GCP console → APIs & Services → OAuth consent screen → PUBLISH APP
("In production").** Until that happens, expect this skill to be needed weekly,
and note `oauth-heartbeat-agent` exists only to reset that clock daily as a
stopgap — publishing the app is what retires both.

---

## 7. Related

- `install/reauth-gsc.sh` — the script (scope-verifying wrapper).
- `install/reauth-google-oauth.sh` — the underlying consent flow.
- `agents/seo-opportunity-agent/lib/collector/refresh-token.py` — `SCOPES` live here.
- `.claude/skills/keep-the-lights-on/SKILL.md` — the tick that detects this.
