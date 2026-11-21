/**
 * lib/deploytool.py: the parsing and checking half of the deploy scripts.
 * Every secret-handling rule is checked here directly: values come from files
 * or stdin, never argv, and no message ever contains one.
 */
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { DEPLOY, runTool } from './harness';

let tmp: string;
beforeAll(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-deploytool-'));
});
afterAll(() => fs.rmSync(tmp, { recursive: true, force: true }));

function envFile(text: string): string {
  const f = path.join(tmp, `env-${crypto.randomBytes(4).toString('hex')}`);
  fs.writeFileSync(f, text);
  return f;
}

function dump(text: string, args: string[] = ['--strict']): Record<string, string> {
  const r = runTool(['env-dump', envFile(text), ...args]);
  if (r.status !== 0) throw new Error(r.stderr);
  const parts = r.stdout.split('\0');
  const out: Record<string, string> = {};
  for (let i = 0; i + 1 < parts.length; i += 2) out[parts[i]] = parts[i + 1];
  return out;
}

/** Independent SCRAM-SHA-256 verifier (RFC 5802 / 7677) for cross-checking. */
function scramVerifier(password: string, salt: Buffer, iterations = 4096): string {
  const salted = crypto.pbkdf2Sync(password, salt, iterations, 32, 'sha256');
  const clientKey = crypto.createHmac('sha256', salted).update('Client Key').digest();
  const storedKey = crypto.createHash('sha256').update(clientKey).digest();
  const serverKey = crypto.createHmac('sha256', salted).update('Server Key').digest();
  return `SCRAM-SHA-256$${iterations}:${salt.toString('base64')}$${storedKey.toString('base64')}:${serverKey.toString('base64')}`;
}

describe('env-dump (deploy.env / secrets.env parser)', () => {
  it('applies shell quoting rules without executing anything', () => {
    expect(dump([
      '# comment',
      '',
      'A=1',
      'export B=\'x y\'',
      'C="a\\"b"',
      "D='it'\\''s'",
      'E=',
      'F=a   # trailing comment',
      "G='semi;colon=and#hash'",
      'H=#starts-a-comment',
      'I=one\\ two',
      'A=2',
    ].join('\n'))).toEqual({
      A: '2', B: 'x y', C: 'a"b', D: "it's", E: '', F: 'a', G: 'semi;colon=and#hash', H: '', I: 'one two',
    });
  });

  it.each([
    ['X=$HOME', 'expansion'],
    ['X="pre$VAR"', 'expansion'],
    ['X=`id`', 'expansion'],
    ['X=a;b', 'metacharacter'],
    ['X=a b', 'whitespace'],
    ["X='unterminated", 'unterminated'],
    ['not an assignment', 'expected KEY=value'],
  ])('rejects %s in strict mode', (line, why) => {
    const r = runTool(['env-dump', envFile(`OK=1\n${line}\n`), '--strict']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('line 2');
    expect(r.stderr).toContain(why);
  });

  it('never echoes a value in an error message', () => {
    const r = runTool(['env-dump', envFile('PASSWORD=TopSecretValue123 extra\n'), '--strict']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('PASSWORD');
    expect(r.stderr).not.toContain('TopSecretValue123');
  });

  it('non-strict lookups skip lines it was not asked about', () => {
    const f = envFile('weird line $(rm -rf /)\nOTHER=$UNPARSEABLE\nWANTED=\'v;1\'\n');
    const r = runTool(['env-dump', f, '--keys', 'WANTED']);
    expect(r.status).toBe(0);
    expect(r.stdout).toBe('WANTED\0v;1\0');
  });

  it('--check prints nothing', () => {
    const r = runTool(['env-dump', envFile('A=1\n'), '--strict', '--check']);
    expect(r).toMatchObject({ status: 0, stdout: '' });
  });
});

describe('DSNs and SCRAM verifiers', () => {
  const salt = Buffer.from('0123456789abcdef');
  const saltB64 = salt.toString('base64');

  it('matches an independent SCRAM-SHA-256 implementation', () => {
    const password = 'Fixture-Password_0123456789';
    const r = runTool(['role-verifier', '--dsn-file', '-', '--salt-b64', saltB64],
      { input: `postgresql://site_app:${password}@db.example.test:5432/site?sslmode=require` });
    expect(r.status).toBe(0);
    expect(r.stdout.trim()).toBe(scramVerifier(password, salt));
    expect(r.stdout + r.stderr).not.toContain(password);
  });

  it('decodes a percent-encoded password and libpq key=value DSNs', () => {
    const pw = 'p@ss/word:with%chars0123';
    const url = `postgres://site_app:${encodeURIComponent(pw)}@db.example.test/site`;
    const a = runTool(['role-verifier', '--dsn-file', '-', '--salt-b64', saltB64, '--expect-user', 'site_app'], { input: url });
    expect(a.stdout.trim()).toBe(scramVerifier(pw, salt));
    const kv = `host=db.example.test port=5432 dbname=site user=site_app password='${pw.replace(/'/g, "\\'")}' sslmode=require`;
    const b = runTool(['role-verifier', '--dsn-file', '-', '--salt-b64', saltB64, '--expect-db', 'site'], { input: kv });
    expect(b.stdout.trim()).toBe(scramVerifier(pw, salt));
  });

  it('refuses a DSN for another role or database (exit 3), warns on another host', () => {
    const dsn = 'postgresql://other_role:Long-Enough-Password-1@db.example.test:5432/site';
    const user = runTool(['role-verifier', '--dsn-file', '-', '--expect-user', 'site_app'], { input: dsn });
    expect(user.status).toBe(3);
    expect(user.stderr).toContain("'other_role'");
    expect(user.stderr).not.toContain('Long-Enough-Password-1');
    const db = runTool(['role-verifier', '--dsn-file', '-', '--expect-db', 'other'], { input: dsn });
    expect(db.status).toBe(3);
    const host = runTool(['role-verifier', '--dsn-file', '-', '--expect-host', 'elsewhere.test'], { input: dsn });
    expect(host.status).toBe(0);
    expect(host.stderr).toContain('WARN');
  });

  it.each([
    ['postgresql://site_app@db.example.test/site', 'no password'],
    ['postgresql://site_app:short@db.example.test/site', 'shorter than 16'],
    ['postgresql://site_app:Ünïcode-password-12345@db.example.test/site', 'printable ASCII'],
  ])('refuses %s', (dsn, why) => {
    const r = runTool(['role-verifier', '--dsn-file', '-'], { input: dsn });
    expect(r.status).toBe(2);
    expect(r.stderr).toContain(why);
  });

  it('dsn-info checks the role and database a DSN logs in to (exit 3), never printing the password', () => {
    const dsn = 'postgresql://site_agents:Long-Enough-Password-1@db.example.test:5432/site';
    const ok = runTool(['dsn-info', '--dsn-file', '-', '--expect-user', 'site_agents', '--expect-db', 'site'], { input: dsn });
    expect(ok.status).toBe(0);
    const user = runTool(['dsn-info', '--dsn-file', '-', '--expect-user', 'site_owner', '--label', 'the owner DSN'], { input: dsn });
    expect(user.status).toBe(3);
    expect(user.stderr).toContain("the owner DSN: the DSN logs in as 'site_agents', expected role 'site_owner'");
    const db = runTool(['dsn-info', '--dsn-file', '-', '--expect-db', 'othersite'], { input: dsn });
    expect(db.status).toBe(3);
    for (const r of [ok, user, db]) expect(r.stdout + r.stderr).not.toContain('Long-Enough-Password-1');
  });

  it('dsn-info describes a DSN without its password', () => {
    const f = path.join(tmp, 'dsn');
    fs.writeFileSync(f, 'postgresql://u:Secret-Password-123456@H.example.test:6432/db?sslmode=require\n', { mode: 0o600 });
    const r = runTool(['dsn-info', '--dsn-file', f]);
    expect(JSON.parse(r.stdout)).toEqual({ user: 'u', host: 'h.example.test', port: 6432, dbname: 'db', sslmode: 'require', has_password: true });
    expect(r.stdout).not.toContain('Secret-Password');
  });

  it('new-dsn writes a 0600 file with a working DSN and prints its verifier', () => {
    const out = path.join(tmp, 'new-dsns.env');
    fs.rmSync(out, { force: true });
    const r = runTool(['new-dsn', '--user', 'site_app', '--host', 'db.example.test', '--port', '5432', '--db', 'site',
      '--var', 'DATABASE_URL_SITE_APP', '--out', out]);
    expect(r.status).toBe(0);
    expect(fs.statSync(out).mode & 0o777).toBe(0o600);
    const line = fs.readFileSync(out, 'utf8');
    const m = /^DATABASE_URL_SITE_APP='postgresql:\/\/site_app:([A-Za-z0-9]{32})@db\.example\.test:5432\/site\?sslmode=require'\n$/.exec(line);
    expect(m).not.toBeNull();
    const password = m![1];
    expect(r.stdout + r.stderr).not.toContain(password);
    const v = r.stdout.trim();
    const [, rest] = v.split('$4096:');
    const salt = Buffer.from(rest.split('$')[0], 'base64');
    expect(v).toBe(scramVerifier(password, salt));
    const again = runTool(['new-dsn', '--user', 'site_app', '--host', 'h', '--db', 'site', '--var', 'DATABASE_URL_SITE_APP', '--out', out]);
    expect(again.status).toBe(3);
  });
});

describe('HTML Cache-Control rule (s-maxage <= 600)', () => {
  const check = (cc: string | null, extra: string[] = []) => {
    const r = runTool(['check-cache-header', ...(cc === null ? ['--missing'] : ['--cache-control', cc]), '--max', '600', ...extra]);
    return { status: r.status, ...(JSON.parse(r.stdout) as { ok: boolean; problems: string[] }) };
  };

  it.each([
    ['public, max-age=60, s-maxage=600'],
    ['public, max-age=60, s-maxage=300'],
    ['private, max-age=0'],
    ['no-store'],
    ['no-cache'],
  ])('accepts %s', (cc) => {
    expect(check(cc)).toMatchObject({ status: 0, ok: true });
  });

  it.each([
    [null, 'no Cache-Control'],
    ['public, max-age=60', 'no s-maxage'],
    ['public, max-age=60, s-maxage=601', 's-maxage=601 > 600'],
    ['public, max-age=900, s-maxage=600', 'max-age=900 > 600'],
    ['public, s-maxage=600, s-maxage=3600', 's-maxage=3600 > 600'],
    ['public, s-maxage=abc', 'not a number'],
  ])('rejects %s', (cc, why) => {
    const r = check(cc);
    expect(r.status).toBe(1);
    expect(r.problems.join(' ')).toContain(why);
  });

  it.each([
    ['public, max-age=60, s-maxage=600, stale-while-revalidate=86400', 'stale-while-revalidate=86400 lets a shared cache serve the HTML'],
    ['public, max-age=60, s-maxage=600, stale-if-error=604800', 'stale-if-error=604800'],
    ['public, s-maxage=300, stale-while-revalidate=abc', "stale-while-revalidate='abc' is not a number"],
  ])('rejects %s (serving stale stretches the price window)', (cc, why) => {
    const r = check(cc);
    expect(r.status).toBe(1);
    expect(r.problems.join(' ')).toContain(why);
  });

  it('accepts stale-while-revalidate=0 and stale directives on a response no shared cache stores', () => {
    expect(check('public, max-age=60, s-maxage=600, stale-while-revalidate=0')).toMatchObject({ ok: true });
    expect(check('private, max-age=60, stale-while-revalidate=600')).toMatchObject({ ok: true });
  });

  it('applies the limit to CDN-targeted headers, which a CDN honours instead of Cache-Control', () => {
    const cc = 'public, max-age=60, s-maxage=600';
    const bad = check(cc, ['--targeted', 'CDN-Cache-Control=max-age=3600']);
    expect(bad.status).toBe(1);
    expect(bad.problems).toEqual(['cdn-cache-control: max-age=3600 > 600']);
    expect(check(cc, ['--targeted', 'Cloudflare-CDN-Cache-Control=max-age=300, stale-while-revalidate=60']).problems.join(' '))
      .toContain('cloudflare-cdn-cache-control: stale-while-revalidate=60');
    expect(check(cc, ['--targeted', 'Surrogate-Control=public']).problems.join(' ')).toContain('sets no max-age');
    expect(check(cc, ['--targeted', 'Surrogate-Control=max-age=300'])).toMatchObject({ ok: true });
    expect(check(cc, ['--targeted', 'CDN-Cache-Control=no-store'])).toMatchObject({ ok: true });
    // A CDN header that caches beyond the limit is caught even when Cache-Control keeps shared caches out.
    expect(check('private, no-store', ['--targeted', 'CDN-Cache-Control=max-age=3600']).ok).toBe(false);
    const unknown = runTool(['check-cache-header', '--cache-control', cc, '--targeted', 'X-Other=max-age=1']);
    expect(unknown.status).toBe(2);
  });

  it('rejects HTML a shared cache serves past the limit (Age)', () => {
    const r = check('public, max-age=60, s-maxage=300', ['--age', '900']);
    expect(r.status).toBe(1);
    expect(r.problems.join(' ')).toContain('Age 900s > 600s');
  });

  it('site-cache checks site.json before a build', () => {
    const ok = path.join(tmp, 'site-ok.json');
    fs.writeFileSync(ok, JSON.stringify({ cache: { htmlSMaxAgeSeconds: 600, htmlMaxAgeSeconds: 60 } }));
    expect(runTool(['site-cache', '--site-json', ok, '--max', '600'])).toMatchObject({ status: 0 });
    const dflt = path.join(tmp, 'site-default.json');
    fs.writeFileSync(dflt, JSON.stringify({ schemaVersion: 1 }));
    expect(runTool(['site-cache', '--site-json', dflt]).stdout).toContain('htmlSMaxAgeSeconds=600');
    const bad = path.join(tmp, 'site-bad.json');
    fs.writeFileSync(bad, JSON.stringify({ cache: { htmlSMaxAgeSeconds: 900 } }));
    const r = runTool(['site-cache', '--site-json', bad, '--max', '600']);
    expect(r.status).toBe(3);
    expect(r.stderr).toContain('cache.htmlSMaxAgeSeconds=900 > 600');
    const lower = runTool(['site-cache', '--site-json', ok, '--max', '300']);
    expect(lower.status).toBe(3);
    const weird = path.join(tmp, 'site-weird.json');
    fs.writeFileSync(weird, JSON.stringify({ cache: { htmlSMaxAgeSeconds: true } }));
    expect(runTool(['site-cache', '--site-json', weird]).status).toBe(2);
  });
});

describe('render-dockerfile', () => {
  const base = ['render-dockerfile', '--template', path.join(DEPLOY, 'Dockerfile.azure.tmpl'), '--app-name', 'site',
    '--kit-version', '9.9.9', '--node-image', 'node:22-alpine', '--port', '8080', '--server-entry', 'dist/src/index.js',
    '--build-script', 'build', '--health-path', '/health'];

  it('fills every placeholder and copies the extra paths into both stages', () => {
    const r = runTool([...base, '--extra-paths', 'db content/', '--out', '-']);
    expect(r.status).toBe(0);
    expect(r.stdout).not.toMatch(/\{\{|\}\}/);
    expect(r.stdout.split('\n')[0]).toBe('# rendered-from: affiliate-kit/deploy/Dockerfile.azure.tmpl (kit 9.9.9)');
    expect(r.stdout).toContain('COPY db ./db\nCOPY content ./content');
    expect(r.stdout).toContain('COPY --from=build /app/db ./db\nCOPY --from=build /app/content ./content');
    // The build stage makes every path the runtime stage copies readable by the
    // server's user, whatever the checkout's file modes: after the build, before
    // the runtime stage.
    const chmod = 'RUN chmod -R a+rX,go-w package.json site.json dist vendor/affiliate-kit db content\n';
    expect(r.stdout).toContain(chmod);
    expect(r.stdout.indexOf(chmod)).toBeGreaterThan(r.stdout.indexOf('RUN npm run build'));
    expect(r.stdout.indexOf(chmod)).toBeLessThan(r.stdout.indexOf('FROM ${NODE_IMAGE} AS production'));
  });

  it('with no extra paths the chmod names the four base paths only (and adds no write bit)', () => {
    const r = runTool([...base, '--extra-paths', '', '--out', '-']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('RUN chmod -R a+rX,go-w package.json site.json dist vendor/affiliate-kit\n');
    expect(r.stdout).not.toMatch(/\{\{|\}\}/);
  });

  it.each([
    [['--extra-paths', '../outside'], 'plain relative path'],
    [['--extra-paths', '/etc'], 'plain relative path'],
    [['--server-entry', '../x.js'], 'SERVER_ENTRY'],
    [['--health-path', 'health'], 'HEALTH_PATH'],
    [['--node-image', 'node:22 && curl evil'], 'NODE_IMAGE'],
    [['--build-script', 'build; rm -rf /'], 'BUILD_SCRIPT'],
  ])('rejects %j', (extra, why) => {
    const args = [...base];
    for (let i = 0; i < extra.length; i += 2) {
      const at = args.indexOf(extra[i]);
      if (at >= 0) args[at + 1] = extra[i + 1];
      else args.push(extra[i], extra[i + 1]);
    }
    const r = runTool([...args, '--out', '-']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain(why);
  });

  it('--check exits 3 with a diff when the file has drifted', () => {
    const f = path.join(tmp, 'Dockerfile.azure');
    fs.writeFileSync(f, runTool([...base, '--out', '-']).stdout);
    expect(runTool([...base, '--check', f]).status).toBe(0);
    fs.appendFileSync(f, 'RUN echo hand-edited\n');
    const r = runTool([...base, '--check', f]);
    expect(r.status).toBe(3);
    expect(r.stderr).toContain('+++ rendered from the kit template');
    expect(r.stderr).toContain('-RUN echo hand-edited');
  });
});

describe('image-users (azure/teardown.sh --with-images)', () => {
  const id = (rg: string, kind: string, name: string) => `/subscriptions/s/resourceGroups/${rg}/providers/Microsoft.App/${kind}/${name}`;
  const args = ['image-users', '--registry', 'acr.azurecr.io', '--repository', 'kitexample', '--exclude-app', 'site-rg/kitexample'];
  const users = (...docs: unknown[]) => runTool(args, { input: docs.map((d) => JSON.stringify(d)).join('\n') });

  it('names every other app or job that runs an image from the repository: any tag, a digest, no tag, an init container', () => {
    const r = users(
      [
        { id: id('SITE-RG', 'containerApps', 'kitexample'), images: [['acr.azurecr.io/kitexample:t1'], null] },
        { id: id('other-rg', 'containerApps', 'kitexample'), images: [['ACR.azurecr.io/kitexample:t2'], null] },
        { id: id('site-rg', 'containerApps', 'b'), images: [['nginx'], ['acr.azurecr.io/kitexample@sha256:0123']] },
        { id: id('site-rg', 'containerApps', 'c'), images: [['acr.azurecr.io/kitexample-x:t1', 'other.azurecr.io/kitexample:t1'], null] },
        { id: id('site-rg', 'containerApps', 'd'), images: [null, null] },
      ],
      [{ id: id('site-rg', 'jobs', 'kitexample'), images: [['acr.azurecr.io/kitexample'], null] }],
    );
    expect(r.status).toBe(0);
    expect(r.stdout.trim().split('\n')).toEqual([id('other-rg', 'containerApps', 'kitexample'), id('site-rg', 'containerApps', 'b'), id('site-rg', 'jobs', 'kitexample')]);
  });

  it('prints nothing when only the site\'s own app uses it, and refuses input it cannot read', () => {
    expect(users([{ id: id('site-rg', 'containerApps', 'kitexample'), images: [['acr.azurecr.io/kitexample:t1'], null] }], []).stdout).toBe('');
    expect(runTool(args, { input: '' }).stdout).toBe('');
    expect(runTool(args, { input: 'ERROR: not json' }).status).toBe(2);
    expect(runTool(args, { input: '{"id": "x"}' }).status).toBe(2);
    expect(runTool(['image-users', '--registry', 'r', '--repository', 'p', '--exclude-app', 'no-slash'], { input: '[]' }).status).toBe(2);
  });
});

describe('JSON documents', () => {
  const taskdef = ['ecs-taskdef', '--family', 'site', '--container-name', 'site', '--image', 'acct.dkr.ecr/site:1',
    '--cpu-units', '512', '--memory-mib', '1024', '--port', '8080', '--exec-role-arn', 'arn:aws:iam::1:role/e',
    '--task-role-arn', 'arn:aws:iam::1:role/t', '--log-group', '/ecs/x', '--region', 'us-east-1', '--stream-prefix', 'site',
    '--env', 'PORT=8080', '--env', 'NODE_ENV=production', '--secret', 'DATABASE_URL=arn:aws:secretsmanager:us-east-1:1:secret:s-AbCdEf:database-url::'];

  it('ecs-taskdef builds a Fargate task definition with secrets by reference', () => {
    const r = runTool([...taskdef, '--out', '-']);
    expect(r.status).toBe(0);
    const doc = JSON.parse(r.stdout);
    expect(doc).toMatchObject({ family: 'site', cpu: '512', memory: '1024', networkMode: 'awsvpc', requiresCompatibilities: ['FARGATE'] });
    const c = doc.containerDefinitions[0];
    expect(c.portMappings).toEqual([{ containerPort: 8080, protocol: 'tcp' }]);
    expect(c.environment).toEqual([{ name: 'PORT', value: '8080' }, { name: 'NODE_ENV', value: 'production' }]);
    expect(c.secrets).toEqual([{ name: 'DATABASE_URL', valueFrom: 'arn:aws:secretsmanager:us-east-1:1:secret:s-AbCdEf:database-url::' }]);
    expect(c.healthCheck.command[1]).toContain('http://127.0.0.1:8080/health');
  });

  it.each([
    [['--memory-mib', '768'], 'no 512 CPU units / 768 MiB'],
    [['--secret', 'DATABASE_URL=plain-value'], 'must reference an ARN'],
    [['--env', 'DATABASE_URL=x'], 'both as plain env and as secrets'],
  ])('ecs-taskdef refuses %j', (extra, why) => {
    const args = [...taskdef];
    if (extra[0] === '--memory-mib') args[args.indexOf('--memory-mib') + 1] = extra[1];
    else if (extra[0] === '--secret') args[args.lastIndexOf('--secret') + 1] = extra[1];
    else args.push(...extra);
    const r = runTool([...args, '--out', '-']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain(why);
  });

  it('json-from-files writes a 0600 secret document', () => {
    const v = path.join(tmp, 'v');
    fs.writeFileSync(v, 'postgresql://a:Long-Password-Value-1@h/d\n');
    const out = path.join(tmp, 'secrets.json');
    expect(runTool(['json-from-files', '--out', out, `database-url=${v}`]).status).toBe(0);
    expect(fs.statSync(out).mode & 0o777).toBe(0o600);
    expect(JSON.parse(fs.readFileSync(out, 'utf8'))).toEqual({ 'database-url': 'postgresql://a:Long-Password-Value-1@h/d' });
    fs.writeFileSync(v, '');
    expect(runTool(['json-from-files', '--out', out, `database-url=${v}`]).status).toBe(2);
  });

  it('scaling-policy tracks average CPU', () => {
    const r = runTool(['scaling-policy', '--target-cpu', '60', '--out', '-']);
    expect(JSON.parse(r.stdout)).toEqual({
      TargetValue: 60, PredefinedMetricSpecification: { PredefinedMetricType: 'ECSServiceAverageCPUUtilization' },
      ScaleOutCooldown: 60, ScaleInCooldown: 300,
    });
    expect(runTool(['scaling-policy', '--target-cpu', '95']).status).toBe(2);
  });
});
