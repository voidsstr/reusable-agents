/**
 * Azure and AWS stay parallel (reusable-agents CLAUDE.md, "Infra scripts are
 * the source of truth"): one deploy.env, the same container env contract, the
 * same size and replica bounds, the same image, gate, migrations, SQL and smoke
 * check. Also: the deploy.env contract in common.sh and deploy.env.example agree,
 * and no deploy code names a site.
 */
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import {
  BASH, DEPLOY, dryCommands, makeSandbox, oneCommand, removeSandbox, runScript, runTool, shownFile, sqlBlocks,
  type RunResult, type Sandbox,
} from './harness';

let sb: Sandbox;
let az: RunResult;
let aws: RunResult;
let azProv: RunResult;
let awsProv: RunResult;
beforeAll(() => {
  sb = makeSandbox();
  az = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--dry-run', '--image-tag', 'parity-1']);
  aws = runScript(sb, 'aws/deploy.sh', ['--env', sb.envFile, '--dry-run', '--image-tag', 'parity-1']);
  azProv = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, '--dry-run', 'database']);
  awsProv = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'database']);
});
afterAll(() => removeSandbox(sb));

function azureEnv(out: string): Map<string, string> {
  const upd = oneCommand(out, 'az containerapp update ');
  const vars = upd.split('--replace-env-vars ')[1].split(' -o none')[0].split(' ');
  return new Map(vars.map((v) => [v.split('=')[0], v.slice(v.indexOf('=') + 1)]));
}

function awsEnv(out: string): Map<string, string> {
  const c = JSON.parse(shownFile(out, 'taskdef.json')).containerDefinitions[0];
  const m = new Map<string, string>();
  for (const e of c.environment) m.set(e.name, e.value);
  for (const s of c.secrets) m.set(s.name, `secret:${s.valueFrom.split(':').slice(-3)[0]}`);
  return m;
}

describe('the same deploy.env drives both clouds the same way', () => {
  it('both runs succeed', () => {
    for (const r of [az, aws, azProv, awsProv]) expect(r.status).toBe(0);
  });

  it('the container gets the same env names, values and secret references', () => {
    const a = azureEnv(az.stdout);
    const w = awsEnv(aws.stdout);
    expect([...a.keys()].sort()).toEqual([...w.keys()].sort());
    for (const [k, v] of a) {
      if (v.startsWith('secretref:')) expect(w.get(k)).toBe(`secret:${v.slice('secretref:'.length)}`);
      else expect(w.get(k)).toBe(v);
    }
  });

  it('the same size and replica bounds: 0.5 vCPU / 1 GiB = 512 / 1024, 1..3', () => {
    const upd = oneCommand(az.stdout, 'az containerapp update ');
    expect(upd).toContain('--cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 3');
    const td = JSON.parse(shownFile(aws.stdout, 'taskdef.json'));
    expect([td.cpu, td.memory]).toEqual(['512', '1024']);
    expect(oneCommand(aws.stdout, 'aws application-autoscaling register-scalable-target ')).toContain('--min-capacity 1 --max-capacity 3');
  });

  it('the same gate, Dockerfile, build labels and migration command', () => {
    const strip = (l: string) => l.replace(/--tag \S+/, '').replace(/image\.created=\S+/, '');
    expect(strip(oneCommand(az.stdout, 'docker build '))).toBe(strip(oneCommand(aws.stdout, 'docker build ')));
    expect(oneCommand(az.stdout, 'bash ')).toBe(oneCommand(aws.stdout, 'bash '));
    const mig = (out: string) => dryCommands(out, 'DATABASE_URL=')[0].replace(/ \S+:parity-1 /, ' IMAGE ');
    expect(mig(az.stdout)).toBe(mig(aws.stdout));
  });

  it('the same smoke paths and HTML cache limit', () => {
    const paths = (out: string) => dryCommands(out).filter((l) => l.includes('deploytool.py smoke'))
      .map((l) => l.slice(l.indexOf('--max-shared-age')).replace(/--connect-to \S+ /, ''));
    expect(paths(az.stdout)).toEqual(paths(aws.stdout));
  });

  it('the same role and grant SQL (only the server differs)', () => {
    expect(sqlBlocks(azProv.stdout)).toEqual(sqlBlocks(awsProv.stdout));
  });

  it('the same flags', () => {
    const flags = (s: string) => new Set(fs.readFileSync(path.join(DEPLOY, s), 'utf8').match(/^ {4}(--[a-z-]+)[=)*]/gm)
      ?.map((m) => m.trim().replace(/[=)*]$/, '')));
    for (const s of ['deploy.sh', 'teardown.sh']) expect(flags(`aws/${s}`)).toEqual(flags(`azure/${s}`));
    const az = flags('azure/provision.sh');
    const aw = flags('aws/provision.sh');
    expect([...aw].filter((f) => !az.has(f))).toEqual([]);
    expect([...az].filter((f) => !aw.has(f)).sort()).toEqual(['--skip-dns-check']);
  });
});

describe('the deploy.env contract', () => {
  function knownKeys(): string[] {
    const r = spawnSync(BASH, ['-c', `source "${path.join(DEPLOY, 'lib/common.sh')}"; printf '%s\\n' "\${KD_KNOWN_KEYS[@]}"`],
      { encoding: 'utf8' });
    expect(r.status).toBe(0);
    return r.stdout.trim().split('\n').sort();
  }

  it('deploy.env.example documents exactly the keys the scripts accept, and parses strictly', () => {
    const r = runTool(['env-dump', path.join(DEPLOY, 'deploy.env.example'), '--strict']);
    expect(r.status).toBe(0);
    const keys = r.stdout.split('\0').filter((_, i) => i % 2 === 0 && _ !== '').sort();
    expect(keys).toEqual(knownKeys());
  });

  it('deploy.env.example passes validation (its site is a placeholder, not a real one)', () => {
    const ex = path.join(sb.fx, 'example.env');
    fs.copyFileSync(path.join(DEPLOY, 'deploy.env.example'), ex);
    const r = runScript(sb, 'azure/provision.sh', ['--env', ex, '--dry-run', 'check']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('app examplepicks in nsc-apps/nsc-apps-env');
  });

  it.each([
    ['MIN_REPLICAS=1', 'MIN_REPLICAS=0', 'scale-to-zero is not allowed'],
    ['MEMORY=1Gi', 'MEMORY=2Gi', 'MEMORY must be twice CPU'],
    ['APP_NAME=kitexample', 'APP_NAME=Kit_Example', 'APP_NAME'],
    ['APP_ENV="NODE_ENV=production', 'APP_ENV="DB_PASSWORD=x NODE_ENV=production', 'looks like a secret'],
    ['APP_ENV="NODE_ENV=production', 'APP_ENV="UPSTREAM=https://u:p@h NODE_ENV=production', 'credentials in a URL'],
    ['APP_SECRET_ENV="DATABASE_URL=database-url"', 'APP_SECRET_ENV="DATABASE_URL=db-url"', 'which APP_SECRETS does not define'],
    ['DB_APP_ROLE=kitexample_app', 'DB_APP_ROLE=kitexample_agents', 'must be distinct'],
    ['DB_APP_STATEMENT_TIMEOUT=15s', 'DB_APP_STATEMENT_TIMEOUT=0', 'positive Postgres duration'],
    ['DB_APP_CONNECTION_LIMIT=20', 'DB_APP_CONNECTION_LIMIT=10', 'exceeds DB_APP_CONNECTION_LIMIT'],
    ['SMOKE_PATHS="/ /health', 'SMOKE_PATHS="health /', 'must be /path'],
    ['MIGRATE_COMMAND="node dist/src/migrate.js"', 'MIGRATE_COMMAND="node x.js; curl evil"', 'run without a shell'],
    ['SITE_HOSTNAMES="kit-example.test www.kit-example.test"', 'SITE_HOSTNAMES="kit-example.test other.test"', 'not kit-example.test or a subdomain'],
    ['TARGET_PORT=8080', 'TARGET_PORT=8080\nTYPO_KEY=1', 'unknown key TYPO_KEY'],
    ['TARGET_PORT=8080', 'TARGET_PORT=$PORT', 'expansion'],
    ['TARGET_PORT=8080', 'TARGET_PORT=8080\nDB_PUBLISHER_GRANTS="UPDATE(status):*"', 'column privileges need named tables'],
    ['TARGET_PORT=8080', 'TARGET_PORT=8080\nDB_PUBLISHER_GRANTS="DELETE(status):editorial_articles"', 'must be PRIVILEGES:TABLES'],
    ['TARGET_PORT=8080', 'TARGET_PORT=8080\nDB_PUBLISHER_GRANTS="SELECT,:editorial_articles"', 'must be PRIVILEGES:TABLES'],
    ['TARGET_PORT=8080', 'TARGET_PORT=8080\nDB_PUBLISHER_GRANTS="UPDATE(Status):editorial_articles"', 'must be PRIVILEGES:TABLES'],
    ['TARGET_PORT=8080', 'TARGET_PORT=8080\nDB_PUBLISHER_GRANTS="SELECT:a:b"', 'TABLES must be'],
    ['AZ_DB_ADMIN_PASSWORD=env:KIT_TEST_PG_ADMIN_PASSWORD', 'AZ_DB_ADMIN_PASSWORD=hunter2hunter2', 'must be env:NAME or file:PATH'],
  ])('rejects %s -> %s', (from, to, why) => {
    const f = path.join(sb.fx, 'bad.env');
    const text = fs.readFileSync(sb.envFile, 'utf8');
    expect(text).toContain(from);
    fs.writeFileSync(f, text.replace(from, to));
    const r = runScript(sb, 'azure/provision.sh', ['--env', f, '--dry-run', 'check']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain(why);
    expect(r.calls).toEqual([]);
  });

  it('accepts column privileges and renders them as column grants', () => {
    const f = path.join(sb.fx, 'cols.env');
    fs.writeFileSync(f, `${fs.readFileSync(sb.envFile, 'utf8')}DB_PUBLISHER_GRANTS="select,UPDATE(status,published_at):editorial_articles INSERT(slug):editorial_articles,drafts"\n`);
    const r = runScript(sb, 'azure/provision.sh', ['--env', f, '--dry-run', 'database']);
    expect(r.status).toBe(0);
    const db = sqlBlocks(r.stdout)[1];
    expect(db).toContain(`EXECUTE format('GRANT SELECT, UPDATE ("status", "published_at") ON public.%I TO %I', 'editorial_articles', 'kitexample_publisher');`);
    expect(db).toContain(`EXECUTE format('GRANT INSERT ("slug") ON public.%I TO %I', 'drafts', 'kitexample_publisher');`);
  });

  it('a value in the caller environment never overrides deploy.env', () => {
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, '--dry-run', 'app'], { MIN_REPLICAS: '0', CPU: '4', APP_NAME: 'x' });
    expect(r.status).toBe(0);
    expect(oneCommand(r.stdout, 'az containerapp create ')).toContain('--name kitexample');
    expect(oneCommand(r.stdout, 'az containerapp create ')).toContain('--cpu 0.5 --memory 1Gi --min-replicas 1');
  });
});

describe('framework-first: no site names in the deploy templates', () => {
  it('no code line names a specific site', () => {
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, e.name);
        if (e.isDirectory()) walk(p);
        else if (/\.(sh|py|tmpl|example)$/.test(e.name)) {
          fs.readFileSync(p, 'utf8').split('\n').forEach((line, i) => {
            const code = line.replace(/^\s*#.*$/, '').replace(/\s#\s.*$/, '');
            if (/specpicks|aisleprompt|runtimepicks/i.test(code)) offenders.push(`${path.relative(DEPLOY, p)}:${i + 1}: ${line.trim()}`);
          });
        }
      }
    };
    walk(DEPLOY);
    expect(offenders).toEqual([]);
  });
});
