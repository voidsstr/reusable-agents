#!/usr/bin/env bash
# Rotate the Azure Postgres flexible-server admin password and every consumer
# of it on this fleet: the host's env files and the Container Apps whose
# DATABASE_URL connects as the admin user.
#
#   bash install/rotate-db-password.sh --dry-run   # list what would change
#   bash install/rotate-db-password.sh             # rotate
#
# Knobs (environment):
#   ROTATE_PG_SERVER   flexible-server name        (default nscappsdb)
#   ROTATE_PG_RG       its resource group          (default nsc-apps)
#   ROTATE_PG_ADMIN    admin role                  (default nscadmin)
#   ROTATE_ENV_FILES   host files holding DSNs     (default secrets.env + ~/.aisleprompt-kitchen.env)
#   ROTATE_SECRET_NAME Container Apps secret name  (default database-url)
#   ROTATE_RESTART_UNITS long-running user services that load secrets.env
#                      (default reusable-agents-api + reusable-agents-host-worker)
#
# Why it exists (2026-10-03): the admin password had been committed to the
# public reusable-agents repo and to ~200 files in the private site repos.
# Those files now read DATABASE_URL_<SITE> from secrets.env, so a rotation is
# a change to secrets.env plus the apps, which this script does in one pass.
#
# Apps get DATABASE_URL as `secretref:<ROTATE_SECRET_NAME>` rather than an
# inline env value, so the DSN is not readable from the app's template. A later
# `<repo>/azure/deploy.sh --env-vars "DATABASE_URL=..."` would turn it back into
# an inline value; pass nothing for DATABASE_URL there.
#
# The password is never printed: it lives in a 0600 file under a 0700 temp dir
# and reaches az through `@file` arguments, then the dir is shredded.
#
# AWS parity: install/deploy-aws.sh keeps the DSN in Secrets Manager. The
# equivalent there is `aws rds modify-db-instance --master-user-password`,
# `aws secretsmanager put-secret-value` and a forced new ECS deployment; that
# path is not scripted yet because no AWS stack is live.
set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

SERVER="${ROTATE_PG_SERVER:-nscappsdb}"
RG="${ROTATE_PG_RG:-nsc-apps}"
ADMIN="${ROTATE_PG_ADMIN:-nscadmin}"
SECRET_NAME="${ROTATE_SECRET_NAME:-database-url}"
ENV_FILES="${ROTATE_ENV_FILES:-$HOME/.reusable-agents/secrets.env $HOME/.aisleprompt-kitchen.env}"
HOST="${SERVER}.postgres.database.azure.com"

set -a
# shellcheck disable=SC1091
. "$HOME/.reusable-agents/secrets.env"
set +a

WORK="$(mktemp -d)"
chmod 700 "$WORK"
cleanup() { find "$WORK" -type f -exec shred -u {} + 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT

export SERVER RG ADMIN SECRET_NAME ENV_FILES HOST WORK DRY_RUN

python3 - <<'PY'
import json, os, re, secrets, string, subprocess, sys, time
from urllib.parse import urlparse

SERVER, RG, ADMIN, SECRET = os.environ['SERVER'], os.environ['RG'], os.environ['ADMIN'], os.environ['SECRET_NAME']
HOST, WORK, DRY = os.environ['HOST'], os.environ['WORK'], os.environ['DRY_RUN'] == '1'
ENV_FILES = [os.path.expanduser(p) for p in os.environ['ENV_FILES'].split()]

def az(*args, check=True):
    r = subprocess.run(['az', *args, '-o', 'json'], capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f'az {args[0]} {args[1] if len(args) > 1 else ""} failed: {r.stderr.strip()[:300]}')
    return json.loads(r.stdout or 'null')

def secret_file(name, content):
    p = os.path.join(WORK, name)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write(content)
    return p

# The current password comes from any DSN on this host that connects as ADMIN to HOST.
old = None
for k, v in os.environ.items():
    if k.startswith('DATABASE_URL') and HOST in v:
        u = urlparse(v)
        if u.username == ADMIN and u.password:
            old = u.password
            break
if not old:
    sys.exit(f'no DATABASE_URL* in secrets.env connects as {ADMIN}@{HOST}')

# Consumers: Container Apps (inline env or secretref) and host files.
apps = []
for a in az('containerapp', 'list', '--query', '[].{n:name,g:resourceGroup}'):
    d = az('containerapp', 'show', '-n', a['n'], '-g', a['g'])
    env = {e['name']: e for c in d['properties']['template']['containers'] for e in c.get('env', [])}
    e = env.get('DATABASE_URL')
    if not e:
        continue
    if e.get('secretRef'):
        vals = az('containerapp', 'secret', 'list', '-n', a['n'], '-g', a['g'], '--show-values')
        dsn = next((s.get('value') or '' for s in vals if s['name'] == e['secretRef']), '')
    else:
        dsn = e.get('value') or ''
    u = urlparse(dsn) if dsn.startswith('postgres') else None
    if u and u.hostname == HOST and u.username == ADMIN:
        apps.append({'name': a['n'], 'rg': a['g'], 'dsn': dsn, 'db': u.path.lstrip('/'),
                     'secretref': e.get('secretRef'), 'fqdn': (d['properties'].get('configuration') or {}).get('ingress', {}).get('fqdn')})
files = [p for p in ENV_FILES if os.path.isfile(p) and old in open(p).read()]

print(f'server {SERVER} ({RG}), admin {ADMIN}')
print('container apps to update:', ', '.join(f"{a['rg']}/{a['name']} ({a['db']}{', secretref' if a['secretref'] else ', inline env'})" for a in apps) or 'none')
print('host files to update:', ', '.join(files) or 'none')
if DRY:
    print('dry run: nothing changed')
    sys.exit(0)

# 1. New password: 40 chars of [A-Za-z0-9] with upper, lower and digit (Azure's
#    3-of-4 rule). Nothing else: 20 agent units get DATABASE_URL through an
#    entry_command that systemd expands and sh re-parses, so $ ` \\ " break it;
#    / ? # @ % break DSN parsing; ' breaks the single-quoted secrets.env value.
alphabet = string.ascii_letters + string.digits
while True:
    new = ''.join(secrets.choice(alphabet) for _ in range(40))
    if any(c.islower() for c in new) and any(c.isupper() for c in new) and any(c.isdigit() for c in new):
        break
pwfile = secret_file('pw', new)

# 2. Stage each app's secret with the NEW DSN first. Running revisions still
#    read their inline DATABASE_URL, so this changes nothing yet; if staging
#    fails we stop here, before the server password moves.
for a in apps:
    a['new_dsn'] = a['dsn'].replace(f':{old}@', f':{new}@')
    f = secret_file(f"secret-{a['name']}", f"{SECRET}={a['new_dsn']}")
    az('containerapp', 'secret', 'set', '-n', a['name'], '-g', a['rg'], '--secrets', '@' + f)
print(f'staged secret {SECRET} on {len(apps)} app(s)')

# 3. Rotate on the server.
t0 = time.time()
az('postgres', 'flexible-server', 'update', '-g', RG, '-n', SERVER, '--admin-password', '@' + pwfile, '--yes')
print(f'server password changed ({time.time() - t0:.0f}s)')

# 4. Wait until the new password logs in.
import psycopg2
probe = f'postgresql://{ADMIN}:{new}@{HOST}:5432/postgres?sslmode=require'
for i in range(60):
    try:
        psycopg2.connect(probe, connect_timeout=10).close()
        print('new password accepted by the server')
        break
    except Exception as e:
        if i == 59:
            sys.exit(f'new password still rejected after 5 minutes: {type(e).__name__}')
        time.sleep(5)

# 5. Host files: swap the password inside DSNs, keep a 0600 backup.
stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
for p in files:
    src = open(p).read()
    bak = f'{p}.bak-{stamp}-pre-rotation'
    with os.fdopen(os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as fh:
        fh.write(src)
    out = src.replace(f':{old}@{HOST}', f':{new}@{HOST}')
    with open(p, 'w') as fh:
        fh.write(out)
    print(f'updated {p} (backup {os.path.basename(bak)}; delete it once verified)')

# 6. Apps, all at once: point DATABASE_URL at the staged secret (new revision),
#    or restart the active revision when it already references it.
procs = []
for a in apps:
    if a['secretref'] == SECRET:
        revs = az('containerapp', 'revision', 'list', '-n', a['name'], '-g', a['rg'], '--query', '[?properties.active].name')
        cmds = [['az', 'containerapp', 'revision', 'restart', '-n', a['name'], '-g', a['rg'], '--revision', r, '-o', 'none'] for r in revs]
    else:
        cmds = [['az', 'containerapp', 'update', '-n', a['name'], '-g', a['rg'],
                 '--set-env-vars', f'DATABASE_URL=secretref:{SECRET}', '-o', 'none']]
    for c in cmds:
        procs.append((a['name'], subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)))
failed = []
for name, p in procs:
    _, err = p.communicate()
    if p.returncode != 0:
        failed.append(name)
        print(f'{name}: switch FAILED: {err.strip()[:300]}')
    else:
        print(f'{name}: DATABASE_URL -> secretref:{SECRET}, new revision rolling')
if failed:
    sys.exit('fix the failed apps by hand: az containerapp update -n <app> -g <rg> '
             f'--set-env-vars DATABASE_URL=secretref:{SECRET}  (the secret is already staged)')

# 7. Long-running host services read secrets.env once at start.
for unit in os.environ.get('ROTATE_RESTART_UNITS', 'reusable-agents-api.service reusable-agents-host-worker.service').split():
    r = subprocess.run(['systemctl', '--user', 'try-restart', unit], capture_output=True, text=True)
    print(f'restarted {unit}' if r.returncode == 0 else f'{unit}: restart failed: {r.stderr.strip()[:200]}')

# 8. Verify: every DB answers with the new password, every app's latest revision runs and serves.
for db in sorted({a['db'] for a in apps} | {'postgres'}):
    psycopg2.connect(f'postgresql://{ADMIN}:{new}@{HOST}:5432/{db}?sslmode=require', connect_timeout=15).close()
print('host can connect to:', ', '.join(sorted({a['db'] for a in apps})))
import urllib.request
for a in apps:
    state = None
    for _ in range(40):
        d = az('containerapp', 'show', '-n', a['name'], '-g', a['rg'])
        latest = d['properties'].get('latestRevisionName')
        rev = az('containerapp', 'revision', 'show', '-n', a['name'], '-g', a['rg'], '--revision', latest, check=False) or {}
        state = (rev.get('properties') or {}).get('runningState')
        if state in ('Running', 'RunningAtMaxScale'):
            break
        time.sleep(15)
    code = None
    if a['fqdn']:
        try:
            code = urllib.request.urlopen(urllib.request.Request(f"https://{a['fqdn']}/", headers={'User-Agent': 'Mozilla/5.0 rotate-db-password'}), timeout=30).status
        except Exception as e:
            code = getattr(e, 'code', type(e).__name__)
    print(f"{a['name']}: revision {latest} {state}, https://{a['fqdn']}/ -> {code}")
print('done; the old password no longer works anywhere')
PY
