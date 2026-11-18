/**
 * Secret plumbing in REAL mode (no --dry-run), against the recording stubs:
 * values reach az as `name=@file`, aws as `file://`, psql and docker through
 * their environment; the files are 0600 in a 0700 directory and gone when the
 * script exits (also on failure); no argv and no output ever contains a value;
 * role passwords reach Postgres only as SCRAM verifiers of the DSN passwords.
 */
import { spawnSync } from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import {
  BASH, DEPLOY, editedEnv, expectNoSecrets, FAKE_SECRET_VALUES, fixtureSecret, makeSandbox, removeSandbox, runScript,
  setResponses, sha256, type RunResult, type Sandbox, type StubCall,
} from './harness';

let sb: Sandbox;
beforeEach(() => {
  sb = makeSandbox();
});
afterEach(() => removeSandbox(sb));

const AZ = {
  owner: { match: 'tags."affiliate-kit-app"', stdout: 'kitexample\n' },
  account: { match: 'az account show', stdout: '00000000-0000-4000-8000-000000000000\n' },
  appExists: { match: 'az containerapp show --name kitexample --resource-group kit-test-rg-does-not-exist --query name ', stdout: 'kitexample\n' },
  pgHost: { match: 'postgres flexible-server show', stdout: 'kit-test-pg-does-not-exist.postgres.database.azure.com\n' },
  digest: { match: 'az acr repository show', stdout: 'sha256:0123\n' },
  fqdn: { match: '--query properties.configuration.ingress.fqdn', stdout: 'kitexample.test-env.eastus.azurecontainerapps.io\n' },
  latestRev: { match: '--query properties.latestRevisionName', stdout: 'kitexample--rev1\n' },
};

function noSecretsAnywhere(r: RunResult): void {
  expectNoSecrets(r.all);
  expectNoSecrets(JSON.stringify(r.calls.map((c) => c.argv)));
}

function call(r: RunResult, tool: string, prefix: string): StubCall {
  const hits = r.calls.filter((c) => c.tool === tool && `${c.argv.join(' ')}`.startsWith(prefix));
  if (hits.length !== 1) throw new Error(`expected one ${tool} ${prefix} call, got ${hits.length}: ${JSON.stringify(r.calls.map((c) => c.argv))}`);
  return hits[0];
}

function expectPrivateFile(c: StubCall, expectedValue: string): string {
  expect(c.files).toHaveLength(1);
  const f = c.files[0];
  expect(f.mode).toBe('0o600');
  expect(f.dirMode).toBe('0o700');
  expect(f.sha256).toBe(sha256(expectedValue));
  return f.path;
}

/** Check a Postgres SCRAM-SHA-256 verifier against a password (salt and count from the verifier). */
function verifierMatches(verifier: string, password: string): boolean {
  const m = /^SCRAM-SHA-256\$(\d+):([^$]+)\$([^:]+):(.+)$/.exec(verifier);
  if (!m) return false;
  const salted = crypto.pbkdf2Sync(password, Buffer.from(m[2], 'base64'), Number(m[1]), 32, 'sha256');
  const stored = crypto.createHash('sha256').update(crypto.createHmac('sha256', salted).update('Client Key').digest()).digest();
  const server = crypto.createHmac('sha256', salted).update('Server Key').digest();
  return stored.toString('base64') === m[3] && server.toString('base64') === m[4];
}

function dsnPassword(name: string): string {
  return decodeURIComponent(new URL(fixtureSecret(name)).password);
}

describe('azure/provision.sh secrets (real mode)', () => {
  it('passes each value as name=@<0600 file> and removes the file afterwards', () => {
    setResponses(sb, [AZ.owner, AZ.appExists]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'secrets']);
    expect(r.status).toBe(0);
    const set = call(r, 'az', 'containerapp secret set');
    const arg = set.argv[set.argv.indexOf('--secrets') + 1];
    expect(arg).toMatch(/^database-url=@\/.+\/database-url$/);
    const p = expectPrivateFile(set, fixtureSecret('KIT_TEST_DATABASE_URL_APP'));
    expect(p.startsWith(sb.tmp)).toBe(true);
    expect(fs.existsSync(p)).toBe(false);
    expect(fs.readdirSync(sb.tmp)).toEqual([]);
    noSecretsAnywhere(r);
    expect(r.calls.some((c) => c.argv.includes('restart'))).toBe(false);
  });

  it('--restart restarts the latest revision', () => {
    setResponses(sb, [AZ.owner, AZ.appExists, AZ.latestRev]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'secrets', '--restart']);
    expect(r.status).toBe(0);
    expect(call(r, 'az', 'containerapp revision restart').argv).toEqual([
      'containerapp', 'revision', 'restart', '--name', 'kitexample', '--resource-group', 'kit-test-rg-does-not-exist',
      '--revision', 'kitexample--rev1', '-o', 'none']);
  });

  it('cleans up the secret files when az fails', () => {
    setResponses(sb, [AZ.owner, AZ.appExists, { match: 'containerapp secret set', exit: 1 }]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'secrets']);
    expect(r.status).toBe(1);
    const set = call(r, 'az', 'containerapp secret set');
    expect(fs.existsSync(set.files[0].path)).toBe(false);
    expect(fs.readdirSync(sb.tmp)).toEqual([]);
    noSecretsAnywhere(r);
  });

  it('exits 3 without calling az when a secret is missing', () => {
    const f = path.join(sb.fx, 'missing.env');
    fs.writeFileSync(f, fs.readFileSync(sb.envFile, 'utf8').replace('env:KIT_TEST_DATABASE_URL_APP"', 'env:NOT_DEFINED_ANYWHERE"'));
    setResponses(sb, [AZ.owner, AZ.appExists]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', f, 'secrets']);
    expect(r.status).toBe(3);
    expect(r.stderr).toContain("container secret 'database-url' (env:NOT_DEFINED_ANYWHERE) is not set");
    expect(r.calls.filter((c) => c.argv.includes('set'))).toEqual([]);
  });

  it('removes the secrets APP_SECRETS no longer lists, keeping the registry password az stored', () => {
    setResponses(sb, [AZ.owner, AZ.appExists,
      { match: 'containerapp secret list', stdout: 'database-url\nold-api-key\nkittestacrdoesnotexistazurecrio-kittestacr\n' },
      { match: 'passwordSecretRef', stdout: 'kittestacrdoesnotexistazurecrio-kittestacr\n' }]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'secrets']);
    expect(r.status).toBe(0);
    expect(call(r, 'az', 'containerapp secret remove').argv).toEqual(['containerapp', 'secret', 'remove', '--name', 'kitexample',
      '--resource-group', 'kit-test-rg-does-not-exist', '--secret-names', 'old-api-key', '-o', 'none']);
    // read before anything is set: a failed listing changes nothing
    const order = r.calls.filter((c) => c.tool === 'az').map((c) => c.argv.slice(0, 3).join(' '));
    expect(order.indexOf('containerapp secret list')).toBeLessThan(order.indexOf('containerapp secret set'));
    noSecretsAnywhere(r);
  });

  it('stops before setting anything when the secret listing fails', () => {
    setResponses(sb, [AZ.owner, AZ.appExists, { match: 'containerapp secret list', exit: 1, stderr: 'ERROR: Connection aborted.\n' }]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'secrets']);
    expect(r.status).toBe(1);
    expect(r.calls.filter((c) => c.argv.includes('set') || c.argv.includes('remove'))).toEqual([]);
  });
});

describe('azure/provision.sh app (real mode, create)', () => {
  it('creates the app with secrets from files and no registry password on the command line', () => {
    setResponses(sb, [AZ.digest, AZ.fqdn]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'app', '--image-tag', 't1']);
    expect(r.status).toBe(0);
    const create = call(r, 'az', 'containerapp create');
    expectPrivateFile(create, fixtureSecret('KIT_TEST_DATABASE_URL_APP'));
    expect(create.argv).toEqual(expect.arrayContaining(['--min-replicas', '1', '--max-replicas', '3', '--cpu', '0.5', '--memory', '1Gi',
      'DATABASE_URL=secretref:database-url', '--registry-server', 'kittestacrdoesnotexist.azurecr.io']));
    expect(create.argv.join(' ')).not.toMatch(/registry-password|registry-username/);
    noSecretsAnywhere(r);
  });

  it('refuses (exit 70, nothing run) when a secret value would reach a command line', () => {
    const f = path.join(sb.fx, 'leak.env');
    fs.writeFileSync(f, fs.readFileSync(sb.envFile, 'utf8')
      .replace('APP_ENV="NODE_ENV=production', 'APP_ENV="LEAK=FakeAppPassw0rdSentinel1 NODE_ENV=production'));
    setResponses(sb, [AZ.digest, AZ.fqdn]);
    for (const args of [['app'], ['app', '--dry-run']]) {
      const r = runScript(sb, 'azure/provision.sh', ['--env', f, ...args, '--image-tag', 't1']);
      expect(r.status).toBe(70);
      expect(r.stderr).toContain('a secret value reached a command line; nothing was run');
      expect(r.calls.filter((c) => c.argv.includes('create'))).toEqual([]);
      expect(r.all).not.toContain('FakeAppPassw0rdSentinel1');
    }
  });
});

describe('the database phase (real mode, psql stub)', () => {
  it('sends the SQL on stdin, the admin password in PGPASSWORD, and role passwords as SCRAM verifiers', () => {
    setResponses(sb, [AZ.pgHost]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'database']);
    expect(r.status).toBe(0);
    const psql = r.calls.filter((c) => c.tool === 'psql');
    expect(psql).toHaveLength(2);
    expect(psql.map((c) => c.argv[0])).toEqual([
      'host=kit-test-pg-does-not-exist.postgres.database.azure.com port=5432 dbname=postgres user=kitadmin sslmode=require application_name=affiliate-kit-deploy',
      'host=kit-test-pg-does-not-exist.postgres.database.azure.com port=5432 dbname=kitexample user=kitadmin sslmode=require application_name=affiliate-kit-deploy',
    ]);
    expect(psql[0].argv.slice(1)).toEqual(['-X', '-q', '-v', 'ON_ERROR_STOP=1', '-f', '-']);
    for (const c of psql) expect(c.env.PGPASSWORD).toBe(sha256('FakeAdminPassw0rdSentinel5'));
    const sql = psql[0].stdin;
    for (const [role, secret] of [
      ['kitexample_owner', 'KIT_TEST_DATABASE_URL_OWNER'], ['kitexample_app', 'KIT_TEST_DATABASE_URL_APP'],
      ['kitexample_agents', 'KIT_TEST_DATABASE_URL'], ['kitexample_publisher', 'KIT_TEST_DATABASE_URL_PUBLISHER'],
    ]) {
      const m = new RegExp(`ALTER ROLE "${role}" WITH PASSWORD '([^']+)';`).exec(sql);
      expect(m).not.toBeNull();
      expect(verifierMatches(m![1], dsnPassword(secret))).toBe(true);
      expect(verifierMatches(m![1], dsnPassword(secret) + 'x')).toBe(false);
    }
    expect(psql[1].stdin).toContain('SET LOCAL ROLE "kitexample_owner";');
    expectNoSecrets(sql);
    expectNoSecrets(psql[1].stdin);
    noSecretsAnywhere(r);
    expect(r.stdout).not.toContain('SCRAM-SHA-256$');
  });

  it('--write-new-dsns generates a password, writes the DSN 0600, and sets the matching verifier', () => {
    setResponses(sb, [AZ.pgHost]);
    const f = path.join(sb.fx, 'gen.env');
    fs.writeFileSync(f, fs.readFileSync(sb.envFile, 'utf8').replace(/^DB_PUBLISHER_DSN=.*$/m, 'DB_PUBLISHER_DSN=env:KIT_GENERATED_PUBLISHER'));
    const out = path.join(sb.root, 'new-dsns.env');
    const r = runScript(sb, 'azure/provision.sh', ['--env', f, 'database', '--write-new-dsns', out]);
    expect(r.status).toBe(0);
    expect(fs.statSync(out).mode & 0o777).toBe(0o600);
    const line = fs.readFileSync(out, 'utf8');
    const m = /^KIT_GENERATED_PUBLISHER='(postgresql:\/\/kitexample_publisher:([A-Za-z0-9]{32})@kit-test-pg-does-not-exist\.postgres\.database\.azure\.com:5432\/kitexample\?sslmode=require)'\n$/.exec(line);
    expect(m).not.toBeNull();
    const sql = r.calls.find((c) => c.tool === 'psql')!.stdin;
    const v = /ALTER ROLE "kitexample_publisher" WITH PASSWORD '([^']+)';/.exec(sql)![1];
    expect(verifierMatches(v, m![2])).toBe(true);
    expect(r.all).not.toContain(m![2]);
  });
});

describe('migrations (real mode, docker stub)', () => {
  it('refuse a DSN that is not the owner role on this site\'s database', () => {
    const f = editedEnv(sb, (t) => t.replace('DB_OWNER_DSN=env:KIT_TEST_DATABASE_URL_OWNER', 'DB_OWNER_DSN=env:KIT_TEST_DATABASE_URL'));
    setResponses(sb, [AZ.digest]);
    const r = runScript(sb, 'azure/deploy.sh', ['--env', f, '--only', 'migrate', '--image-tag', 't1']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain("logs in as 'kitexample_agents', expected role 'kitexample_owner'");
    expect(r.calls.filter((c) => c.tool === 'docker')).toEqual([]);
    noSecretsAnywhere(r);
  });

  it('runs MIGRATE_COMMAND in the image with the owner DSN only in docker\'s environment', () => {
    setResponses(sb, [AZ.digest]);
    const r = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--only', 'migrate', '--image-tag', 't1']);
    expect(r.status).toBe(0);
    const run = call(r, 'docker', 'run');
    expect(run.argv).toEqual(['run', '--rm', '-e', 'DATABASE_URL', 'kittestacrdoesnotexist.azurecr.io/kitexample:t1', 'node', 'dist/src/migrate.js']);
    expect(run.env.DATABASE_URL).toBe(sha256(fixtureSecret('KIT_TEST_DATABASE_URL_OWNER')));
    noSecretsAnywhere(r);
  });
});

describe('the leak guard knows every configured secret, in every script', () => {
  const leaky = () => editedEnv(sb, (t) => t.replace('APP_ENV="NODE_ENV=production', 'APP_ENV="LEAK=FakeAppPassw0rdSentinel1 NODE_ENV=production'));

  it.each([['azure/deploy.sh'], ['aws/deploy.sh']])('%s --only release refuses (exit 70) a secret value in APP_ENV, real and dry', (script) => {
    const f = leaky();
    setResponses(sb, [AZ.owner, { match: '--include TAGS', stdout: 'kitexample\n' }, AZ.digest,
      { match: 'ecr describe-images', stdout: 'sha256:0123\n' }, { match: '--query name -o tsv', stdout: 'kitexample\n' },
      { match: "services[?status=='ACTIVE'].serviceName", stdout: 'kitexample\n' }, { match: 'ingress show', stdout: '8080\n' },
      { match: 'secretsmanager describe-secret', stdout: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:nsc/kitexample-AbCdEf\n' }]);
    for (const extra of [[], ['--dry-run']]) {
      const r = runScript(sb, script, ['--env', f, '--only', 'release', '--image-tag', 't1', ...extra]);
      expect(r.status).toBe(70);
      expect(r.stderr).toContain('a secret value reached a command line; nothing was run');
      expect(r.all).not.toContain('FakeAppPassw0rdSentinel1');
      expect(r.calls.filter((c) => ['update', 'update-service', 'register-task-definition'].includes(c.argv[1]))).toEqual([]);
    }
  });

  it('refuses a secret shorter than 8 characters (too short to be told from text) without printing it', () => {
    const secrets = path.join(sb.fx, 'secrets-short.env');
    fs.writeFileSync(secrets, `${fs.readFileSync(path.join(sb.fx, 'secrets.env'), 'utf8')}KIT_SHORT='abc12'\n`);
    const f = editedEnv(sb, (t) => t.replace(/^SECRETS_FILE=.*$/m, 'SECRETS_FILE=secrets-short.env')
      .replace('APP_SECRETS="database-url=env:KIT_TEST_DATABASE_URL_APP"', 'APP_SECRETS="database-url=env:KIT_TEST_DATABASE_URL_APP api-key=env:KIT_SHORT"'));
    const r = runScript(sb, 'azure/deploy.sh', ['--env', f, '--dry-run', '--smoke-only', '--image-tag', 't1']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('secret env:KIT_SHORT is shorter than 8 characters');
    expect(r.all).not.toContain('abc12');
  });

  it('refuses to run under bash -x, which would print every secret', () => {
    const r = spawnSync(BASH, ['-x', path.join(DEPLOY, 'azure/provision.sh'), '--env', sb.envFile, '--dry-run', 'check'], {
      cwd: sb.fx, encoding: 'utf8', timeout: 60_000,
      env: { PATH: sb.bin, HOME: sb.home, TMPDIR: sb.tmp, LANG: 'C.UTF-8', KIT_STUB_LOG: sb.log, KIT_STUB_RESPONSES: sb.responses },
    });
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('refusing to run with xtrace on');
    expectNoSecrets(r.stdout + r.stderr);
  });
});

describe('aws/provision.sh secrets (real mode)', () => {
  it('writes the Secrets Manager document from a 0600 file via file://', () => {
    setResponses(sb, [{ match: 'aws sts get-caller-identity', stdout: '123456789012\n' }]);
    const r = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, 'secrets']);
    expect(r.status).toBe(0);
    const create = call(r, 'aws', 'secretsmanager create-secret');
    const arg = create.argv[create.argv.indexOf('--secret-string') + 1];
    expect(arg).toMatch(/^file:\/\/\/.+\/app-secrets\.json$/);
    const doc = `{"database-url": "${fixtureSecret('KIT_TEST_DATABASE_URL_APP')}"}`;
    const p = expectPrivateFile(create, doc);
    expect(fs.existsSync(p)).toBe(false);
    noSecretsAnywhere(r);
  });

  it('updates an existing secret with put-secret-value', () => {
    setResponses(sb, [{ match: "Tags[?Key=='affiliate-kit-app']", stdout: 'kitexample\n' },
      { match: 'secretsmanager describe-secret', stdout: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:nsc/kitexample-AbCdEf\n' }]);
    const r = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, 'secrets']);
    expect(r.status).toBe(0);
    expect(r.calls.filter((c) => c.argv.includes('create-secret'))).toEqual([]);
    expectPrivateFile(call(r, 'aws', 'secretsmanager put-secret-value'), `{"database-url": "${fixtureSecret('KIT_TEST_DATABASE_URL_APP')}"}`);
  });
});

describe('env:NAME references', () => {
  it('read the exported environment first, then SECRETS_FILE, never a script variable of the same name', () => {
    const f = path.join(sb.fx, 'envref.env');
    fs.writeFileSync(f, fs.readFileSync(sb.envFile, 'utf8').replace('env:KIT_TEST_DATABASE_URL_APP"', 'env:RESTART"'));
    // RESTART is one of provision.sh's own variables, not exported: the secret is missing.
    const a = runScript(sb, 'azure/provision.sh', ['--env', f, '--dry-run', 'app']);
    expect(a.status).toBe(0);
    expect(a.stderr).toContain("secret 'database-url' (env:RESTART) is not set");
    // An exported variable wins over SECRETS_FILE.
    setResponses(sb, [AZ.owner, AZ.appExists]);
    const b = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'secrets'],
      { KIT_TEST_DATABASE_URL_APP: 'postgresql://kitexample_app:From-The-Environment-123@h/kitexample' });
    expect(b.status).toBe(0);
    expectPrivateFile(call(b, 'az', 'containerapp secret set'), 'postgresql://kitexample_app:From-The-Environment-123@h/kitexample');
    expect(b.all).not.toContain('From-The-Environment-123');
  });
});

it('the fake secrets are what the fixtures hold', () => {
  const text = fs.readFileSync(path.join(__dirname, 'fixtures/secrets.env'), 'utf8');
  for (const v of FAKE_SECRET_VALUES) expect(text).toContain(v);
});
