# GlitchTip — self-hosted error tracking for the framework

GlitchTip is a Sentry-API-compatible error tracker. The framework ships
it as an **opt-in companion**: install it if you want crash + error
visibility for your apps, skip it if you don't. When installed, the
framework's `crash-watcher-agent` polls it every 10 minutes and
dispatches `crash-fix` recs to the implementer — closing the
crash → fix → ship loop without human intervention.

**Why GlitchTip vs full Sentry self-hosted:**

| | Full Sentry self-hosted | GlitchTip |
|---|---|---|
| Services | 23+ containers (kafka, clickhouse, snuba, relay, …) | 4 containers (web, worker, postgres, redis) |
| RAM idle | 4-8 GB | ~300-500 MB |
| Boot time | 8-12 min | <1 min |
| SDK | `@sentry/react-native`, `@sentry/python`, etc. | **same SDKs** — DSN format is identical |
| REST API | Sentry contract | **Sentry-compatible** — no agent changes |

**Why GlitchTip vs Sentry SaaS:**

Same SDKs, you own the data, no per-event pricing. Trade-off: you run
the box and arrange the public ingress. (For most internal/small-team
apps this is fine.)

## Install

```bash
cd install/glitchtip
bash install.sh   # idempotent — re-run to refresh images
```

That brings up 4 containers on `http://localhost:8095`:

| Container | Role |
|---|---|
| `glitchtip-web` | Django UI + REST API (port 8095) |
| `glitchtip-worker` | Celery worker (ingest pipeline) + beat scheduler |
| `glitchtip-postgres` | Event + project storage |
| `glitchtip-redis` | Celery broker + cache |

Initial migrations run in a one-shot `glitchtip-migrate` container that
exits when done. First request after boot may 502 for ~20s while
migrations finish.

## Public ingress (required if your app is on real devices)

Apps running on real phones can't reach `localhost:8095` on the
framework host. The DSN endpoint must be publicly reachable. Two
common paths:

### Option A — Cloudflare Tunnel (recommended for self-hosted)

If your domain is on Cloudflare, you can expose GlitchTip via a free
named tunnel with no port-forwarding and automatic TLS.

```bash
# One-time (only if you don't already have cloudflared authed):
cloudflared tunnel login

# Create the tunnel
cloudflared tunnel create glitchtip
cloudflared tunnel route dns glitchtip glitchtip.<your-domain>

# Config file at ~/.cloudflared/config-glitchtip.yml — see
# the example below (replace tunnel UUID with the one returned above)
```

`~/.cloudflared/config-glitchtip.yml`:

```yaml
tunnel: <tunnel-uuid>
credentials-file: /home/<user>/.cloudflared/<tunnel-uuid>.json
ingress:
  - hostname: glitchtip.<your-domain>
    service: http://localhost:8095
    originRequest:
      connectTimeout: 30s
      keepAliveTimeout: 60s
      httpHostHeader: glitchtip.<your-domain>
  - service: http_status:404
```

Run as a systemd user service so it survives reboots — see
`/home/voidsstr/.config/systemd/user/cloudflared-glitchtip.service`
in this repo's setup for the working unit file.

After the tunnel is up, edit `install/glitchtip/.env`:

```bash
GLITCHTIP_DOMAIN=https://glitchtip.<your-domain>
```

then `docker compose -f docker-compose.glitchtip.yml up -d --force-recreate
glitchtip-web glitchtip-worker` so CSRF + email links use the public
hostname.

### Option B — Azure Container Apps (or any cloud)

Deploy the same compose to your cloud of choice. The dashboard's own
`install/deploy-azure.sh` is a reference pattern. Not yet templated
for GlitchTip in this repo; PRs welcome.

## First-run setup (in the GlitchTip UI)

1. Open `https://glitchtip.<your-domain>` (or `http://localhost:8095`
   for dev-only)
2. Register the operator account (first user = superuser by virtue of
   `is_superuser`; you can promote later if needed)
3. **Create organization** — pick a slug like `nsc` or your company name
4. **Create project** — pick the platform (`React Native`, `Python`,
   `Node`, etc.). Note the slug (e.g. `aisleprompt`).
5. Copy the **DSN** shown on the project setup page. Format:
   `https://<public-key>@<host>/<project-id>`
6. **Create an API token** for the crash-watcher agent:
   Profile → Auth Tokens → New, scopes:
   `org:read project:read event:read member:read`
   (bitmask = 9345 if you ever insert directly to the DB)

## Wire the crash-watcher agent

Add to the framework `.env`:

```bash
SENTRY_API_BASE=https://glitchtip.<your-domain>/api/0
SENTRY_API_TOKEN=<token-from-step-6>
SENTRY_ORG_SLUG=<your-org-slug>
SENTRY_PROJECT_SLUG=<your-project-slug>
CRASH_WATCHER_TARGET_SITE=<site-id-the-crashes-belong-to>
```

Register + enable the agent:

```bash
FRAMEWORK_API_URL=http://localhost:8093 \
  bash install/register-agent.sh agents/crash-watcher-agent

curl -X PATCH http://localhost:8093/api/agents/crash-watcher-agent \
  -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

Every 10 min the agent polls
`/api/0/projects/<org>/<proj>/issues/?query=is:unresolved`, dedupes
against its accumulator (rec-memory primitive), and dispatches the new
ones to the implementer. See `agents/crash-watcher-agent/README.md` for
the full loop description (re-emergence handling, top-frame parsing,
etc.).

## Mobile-SDK wiring (worked example: aisleprompt)

This section is the **checklist another session (or another developer)
follows** to wire a React Native / Expo app into the GlitchTip
instance. It captures everything aisleprompt needs to ship crashes to
the local box. The same pattern applies to any RN/Expo app.

### What already exists in `aisleprompt/mobile`

The prior session installed and wired:

- `@sentry/react-native@7.2.0` in `package.json`
- `mobile/App.tsx` — `Sentry.init({ dsn, release, dist, tracesSampleRate, … })` + `Sentry.wrap(App)` at the default export
- `mobile/app.json` — `@sentry/react-native/expo` plugin block with
  `organization: "aisleprompt"`, `project: "aisleprompt-mobile"`,
  `url: "https://sentry.io/"`
- `mobile/app.json` `extra.SENTRY_DSN: ""` placeholder

### What needs to change for GlitchTip (next session's work)

1. **Update the Expo plugin URL** to point at the local GlitchTip:

   In `mobile/app.json` find the `@sentry/react-native/expo` plugin
   block and change:

   ```json
   {
     "organization": "<your-glitchtip-org-slug>",
     "project": "<your-glitchtip-project-slug>",
     "url": "https://glitchtip.<your-domain>/"
   }
   ```

   The plugin uses this URL only at build time for source-map upload.
   The runtime DSN (where events get sent) is set independently below.

2. **Verify EAS env vars are set** (the framework operator already did
   this for aisleprompt — confirm in another session before building):

   ```bash
   cd mobile
   npx eas-cli env:list production
   # Expect to see:
   #   EXPO_PUBLIC_SENTRY_DSN=https://<public-key>@glitchtip.<your-domain>/<project-id>
   #   SENTRY_DSN=<same value>
   ```

   If missing:

   ```bash
   DSN="https://<public-key>@glitchtip.<your-domain>/<project-id>"
   npx eas-cli env:create --name EXPO_PUBLIC_SENTRY_DSN --value "$DSN" \
       --environment production --visibility plaintext
   npx eas-cli env:create --name SENTRY_DSN --value "$DSN" \
       --environment production --visibility plaintext
   ```

3. **Add the build-time source-map upload secrets** so symbolicated
   traces work in the GlitchTip UI:

   ```bash
   # API token from GlitchTip Profile → Auth Tokens, scopes:
   # project:write project:releases org:read
   npx eas-cli env:create --name SENTRY_AUTH_TOKEN --value "<token>" \
       --environment production --visibility secret
   npx eas-cli env:create --name SENTRY_ORG --value "<your-org-slug>" \
       --environment production --visibility plaintext
   npx eas-cli env:create --name SENTRY_PROJECT --value "<your-project-slug>" \
       --environment production --visibility plaintext
   ```

   The Expo Sentry plugin reads these during `eas build` and uploads
   source maps to whatever `url:` is set on the plugin (GlitchTip
   accepts the same source-map upload protocol Sentry does).

4. **Smoke-test before next TestFlight build** — verify the DSN
   resolves and the project ID is correct:

   ```bash
   curl -sS -X POST \
     "https://glitchtip.<your-domain>/api/<project-id>/store/" \
     -H "Content-Type: application/json" \
     -H "X-Sentry-Auth: Sentry sentry_version=7, sentry_key=<public-key>, sentry_client=manual/1.0" \
     -d '{"event_id":"a1b2c3d4e5f600000000000000000000","timestamp":"2026-05-11T15:30:00Z","platform":"javascript","message":"manual smoke test","level":"info"}'
   ```

   If it returns `{"id":"…"}`, the ingest pipeline is working. Confirm
   the event lands in the GlitchTip UI under Issues.

5. **Build + ship**:

   ```bash
   cd mobile
   npx eas-cli build --platform ios --profile production
   ```

   The plugin auto-injects the DSN into the runtime binary. First
   crash from the new build lands in GlitchTip within ~1 min; the
   `crash-watcher-agent` picks it up on its next 10-min tick.

### What this does NOT cover

- Crashes from app versions that shipped **before** the SDK was added
  — these stay in Xcode Organizer / App Store Connect. The crash → fix
  loop only catches forward-going releases.
- Native iOS crashes that happen before JS bridge init (very rare for
  Expo apps; the SDK initializes early enough to catch nearly all
  user-visible crashes).
- React Native source-map symbolication if `SENTRY_AUTH_TOKEN` is not
  set during the build — traces will show minified function names.
  The crash-watcher can usually still find the file but the LLM has
  less context.

## Operations

### Backups

The DB is `glitchtip-postgres` (volume `glitchtip-pgdata`). Snapshot it
the same way you back up other postgres data on this box — `pg_dump`
inside the container, or a volume-level snapshot. There's no critical
state outside postgres; project keys, issues, events are all in there.

### Retention

GlitchTip's default plan retains events forever. For self-hosted, the
periodic celery-beat task `cleanup_old_issues` deletes events older
than `GLITCHTIP_MAX_EVENT_LIFE_DAYS` (default 90). Override in
`.env` if you want shorter retention.

### Logs

```bash
docker logs glitchtip-web -f       # request log
docker logs glitchtip-worker -f    # ingest pipeline + scheduled tasks
```

### Switching to Sentry SaaS later

Zero code change in the agent. Just:

```bash
# In framework .env — remove SENTRY_API_BASE, or set it to:
#   SENTRY_API_BASE=https://sentry.io/api/0
# Re-create the org/project in Sentry, swap the DSN in EAS env vars,
# update the Sentry Expo plugin url back to https://sentry.io/.
```

The crash-watcher agent + the mobile SDK + the implementer dispatch
contract all stay the same.

## Anti-patterns to avoid

- ❌ Putting the DSN in `mobile/app.json` `extra.SENTRY_DSN` as a literal
  value. Use EAS env vars so the same `app.json` works across
  development/staging/production builds.
- ❌ Pointing the SDK at `http://localhost:8095` in a TestFlight build.
  The DSN host must be reachable from the device.
- ❌ Reusing one GlitchTip project for multiple apps. Crash-watcher
  routes one project → one implementer dispatch target via
  `CRASH_WATCHER_TARGET_SITE`. Cross-project crashes need separate
  agent instances.
- ❌ Setting `SENTRY_AUTH_TOKEN` as `plaintext` instead of `secret` in
  EAS — it grants write access to your project.
