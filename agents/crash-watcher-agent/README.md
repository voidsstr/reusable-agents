# crash-watcher-agent

Closes the **crash → fix** loop end-to-end without human intervention.

## Backend: GlitchTip (recommended) or Sentry SaaS

This agent speaks the Sentry REST API, so it works against either:

- **GlitchTip self-hosted** (default for this framework, free, ~300 MB RAM).
  Install via `bash install/glitchtip/install.sh`. Then set
  `SENTRY_API_BASE=http://localhost:8095/api/0` in the framework `.env`.
- **Sentry SaaS** — sign up at https://sentry.io/signup/. Leave
  `SENTRY_API_BASE` unset; defaults to `https://sentry.io/api/0`.

The mobile app's `@sentry/react-native` SDK is unchanged either way —
just swap the DSN.

## Setup (one-time, ~3 minutes)

1. **Pick a backend and create org + project**
   - GlitchTip: `bash install/glitchtip/install.sh`, then open
     http://localhost:8095, register, create org + React Native project
   - Sentry SaaS: https://sentry.io/signup/, create a React Native project
   - Either way, copy the DSN from the project setup page

2. **Wire the DSN into the mobile app's EAS secrets**

   ```bash
   cd ~/development/aisleprompt/mobile
   eas secret:create --name SENTRY_DSN --value "https://<key>@sentry.io/<proj-id>" --type string
   eas secret:create --name SENTRY_AUTH_TOKEN --value "<auth-token>" --type string   # for source-map upload
   eas secret:create --name SENTRY_ORG --value "aisleprompt" --type string
   eas secret:create --name SENTRY_PROJECT --value "aisleprompt-mobile" --type string
   ```

   The DSN is the public client key — safe in the binary. The auth
   token is for build-time source-map upload; never ship to the device.

3. **Create an API token for the watcher agent**
   - GlitchTip: Profile → Auth Tokens → New, scopes
     `org:read project:read event:read member:read`
   - Sentry SaaS: Settings → Developer Settings → New Internal Integration,
     scopes `Issues: Read & Write`, `Events: Read`, `Project: Read`
   - Copy the generated token (one-shot — store now)

4. **Set env vars on the framework host** (in `.env`):

   ```bash
   SENTRY_API_TOKEN=<token-from-step-3>
   SENTRY_ORG_SLUG=aisleprompt
   SENTRY_PROJECT_SLUG=aisleprompt-mobile
   CRASH_WATCHER_TARGET_SITE=aisleprompt
   # Only needed for GlitchTip; omit for sentry.io SaaS
   SENTRY_API_BASE=http://localhost:8095/api/0
   ```

5. **Register the agent + enable the timer**:

   ```bash
   FRAMEWORK_API_URL=http://localhost:8093 \
       bash install/register-all-from-dir.sh agents
   systemctl --user enable --now agent-crash-watcher-agent.timer
   ```

6. **Trigger a test crash from a TestFlight build** to verify the loop:
   - In the mobile app, navigate to a debug-only "Crash test" button (or
     just throw an Error from any handler in a test build)
   - Wait ~5 minutes
   - Check `/api/implementer/queue` — you should see a `crash-fix` rec
     in the accumulator backlog for `crash-watcher-agent`

## How it works

Every 10 minutes the agent:

1. Pulls all unresolved Sentry issues from the last 24 hours
2. Compares against its `state/accumulator.json` (rec-memory pattern)
3. For each NEW issue:
   - Fetches the latest event → top in-app frame → file + line + function
   - Builds a rec describing the crash with the source location as `files`
   - Dispatches to the implementer via `dispatch_now()` with
     `dispatch_kind=crash-fix`
4. The implementer routes through its chain (claude → copilot → azure)
   and ships a commit. The destructive-shrinkage gate + SSR-mismatch
   gate + framework-detection prompt all still apply.
5. When Sentry marks an issue resolved (via Sentry UI or the implementer
   itself), the watcher transitions the accumulator entry to
   `state=implemented`.
6. If a previously-resolved issue REAPPEARS as unresolved (regression),
   the watcher re-dispatches it with `prior fix didn't hold` annotation.

## Failure modes + recovery

- **Sentry rate limits**: `429 Too Many Requests` → the agent surfaces it
  in the run summary and skips that tick. Cron will retry in 10 min.
- **Source-map upload failed in build**: stack traces will show
  minified function names. The implementer can usually still locate
  the file from the bundled path, but quality is lower. Fix by
  re-running EAS build with the auth token configured.
- **Crash is in a third-party SDK** (e.g., react-native-firebase):
  the implementer will defer with `out of scope` because the rec's
  `files` list points outside the app's own source. Manual triage.

## Cost

- Sentry free tier: 5K errors/month
- Watcher's claude/copilot/azure usage: ~one implementer dispatch per
  unique crash, capped at 8 recs per dispatch by build-aider-invocation.
