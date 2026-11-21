/**
 * tests/run-local-suite.sh, the kit's gate runner, driven with fake docker,
 * curl and npx (tests/unit/fakes) so every path runs in milliseconds and
 * offline: the settings, the exit-code contract the deployer classifies
 * (2-8, 78), the per-project and host locks (taken all or nothing), nice, the
 * CPU cap, the freshen step, the artifacts and the cleanup on TERM. Locks, nice
 * and git are real.
 */
import { execFileSync, spawn, spawnSync, type ChildProcess } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const KIT = path.resolve(__dirname, '..', '..');
const RUNNER = path.join(KIT, 'tests', 'run-local-suite.sh');
const FAKES = path.join(__dirname, 'fakes');

interface Sandbox {
  root: string;
  site: string;
  suiteEnv: string;
  locks: string;
  fake: string;
  log: string;
  artifacts: string;
}

const sandboxes: string[] = [];
const holders: ChildProcess[] = [];

/** Kill a lock holder and everything it started (its process group). */
function release(h: ChildProcess): void {
  try { process.kill(-(h.pid as number), 'SIGKILL'); } catch { /* already gone */ }
}

afterAll(() => {
  for (const h of holders) release(h);
  for (const d of sandboxes) fs.rmSync(d, { recursive: true, force: true });
});

function sandbox(suiteEnv: Record<string, string> = {}): Sandbox {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-suite-'));
  sandboxes.push(root);
  const site = path.join(root, 'site');
  fs.mkdirSync(path.join(site, 'tests'), { recursive: true });
  fs.mkdirSync(path.join(site, 'db'), { recursive: true });
  fs.writeFileSync(path.join(site, 'docker-compose.yml'), 'services: {}\n');
  fs.writeFileSync(path.join(site, 'docker-compose.test.yml'), 'services: {}\n');
  fs.writeFileSync(path.join(site, 'db', 'freshen.sql'), 'UPDATE t SET fetched_at = now();\n');
  const env = {
    SUITE_PROJECT: 'fx',
    SUITE_APP_PORT: '10031',
    SUITE_DB_PORT: '5433',
    SUITE_DB_NAME: 'fx_db',
    SUITE_DB_USER: 'fx',
    SUITE_DB_PASSWORD: 'fxpass',
    SUITE_FIXTURE_FRESHEN_SQL: 'db/freshen.sql',
    SUITE_HEALTH_INTERVAL_S: '0',
    SUITE_LOCK_POLL_S: '1',
    ...suiteEnv,
  };
  const suitePath = path.join(site, 'tests', 'suite.env');
  fs.writeFileSync(suitePath, Object.entries(env).map(([k, v]) => `${k}='${v}'`).join('\n') + '\n');
  const fake = path.join(root, 'fake');
  fs.mkdirSync(fake);
  const locks = path.join(root, 'locks');
  fs.mkdirSync(locks);
  return { root, site, suiteEnv: suitePath, locks, fake, log: path.join(fake, 'calls.log'), artifacts: path.join(root, 'artifacts') };
}

function runnerEnv(sb: Sandbox, extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const k of Object.keys(env)) if (/^(SUITE_|KIT_SUITE_|KIT_GATE_|GATE_|FAKE_)/.test(k)) delete env[k];
  return {
    ...env,
    PATH: `${FAKES}:${process.env.PATH}`,
    AGENT_LOCK_DIR: sb.locks,
    FAKE_LOG: sb.log,
    FAKE_DIR: sb.fake,
    FAKE_DB_CONTAINER: 'fx_postgres',
    ...extra,
  };
}

function run(sb: Sandbox, args: string[] = [], extra: Record<string, string> = {}): { code: number; out: string } {
  const r = spawnSync('bash', [RUNNER, '--suite-env', sb.suiteEnv, ...args], { env: runnerEnv(sb, extra), encoding: 'utf8', timeout: 60_000 });
  return { code: r.status ?? -1, out: `${r.stdout}${r.stderr}` };
}

const calls = (sb: Sandbox): string[] => (fs.existsSync(sb.log) ? fs.readFileSync(sb.log, 'utf8').trim().split('\n') : []);
const npxEnv = (sb: Sandbox): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const line of fs.readFileSync(path.join(sb.fake, 'npx.env'), 'utf8').trim().split('\n')) {
    const at = line.indexOf('=');
    const k = line.slice(0, at);
    out[k] = out[k] ? `${out[k]},${line.slice(at + 1)}` : line.slice(at + 1);
  }
  return out;
};

/** Hold an exclusive flock on `file` in a child process until the test ends. */
async function holdLock(file: string): Promise<ChildProcess> {
  // Its own process group, so release() takes the lock (and the sleep) down at once.
  const child = spawn('flock', ['-x', file, '-c', 'echo held; exec sleep 60'], { stdio: ['ignore', 'pipe', 'ignore'], detached: true });
  holders.push(child);
  await new Promise<void>((resolve) => child.stdout!.once('data', () => resolve()));
  return child;
}

const baseNice = Number(execFileSync('nice', { encoding: 'utf8' }).trim());

describe('settings', () => {
  it('--print-config resolves suite.env with the defaults, the derived URLs and a masked DB password', () => {
    const sb = sandbox();
    const r = run(sb, ['--print-config']);
    expect(r.code).toBe(0);
    expect(r.out).toContain(`site_root=${fs.realpathSync(sb.site)}\n`);
    expect(r.out).toContain('url=http://localhost:10031\n');
    expect(r.out).toContain('app_container=fx_backend_test\n');
    expect(r.out).toContain('db_container=fx_postgres\n');
    expect(r.out).toContain('db_url=postgresql://fx:***@127.0.0.1:5433/fx_db\n');
    expect(r.out).toContain('workers=4\n');
    expect(r.out).toContain('nice=10\n');
    expect(r.out).toContain('cpus=2\n');
    expect(r.out).toContain('playwright_args=--workers=4\n');
    expect(r.out).not.toContain('fxpass');
  });

  it('lets the environment win over suite.env, and SUITE_ENV name the file', () => {
    const sb = sandbox();
    const r = spawnSync('bash', [RUNNER, '--print-config', '--grep', 'x'], {
      env: runnerEnv(sb, { SUITE_ENV: sb.suiteEnv, SUITE_WORKERS: '1', SUITE_APP_PORT: '10099', SUITE_CPUS: '' }),
      encoding: 'utf8',
    });
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('url=http://localhost:10099\n');
    expect(r.stdout).toContain('cpus=off\n');
    expect(r.stdout).toContain('playwright_args=--workers=1 --grep x\n');
  });

  it('keeps the caller\'s own --workers', () => {
    const sb = sandbox();
    expect(run(sb, ['--print-config', '--workers=6']).out).toContain('playwright_args=--workers=6\n');
    expect(run(sb, ['--print-config', '-j', '2']).out).toContain('playwright_args=-j 2\n');
  });

  it.each([
    [{ SUITE_PROJECT: 'Bad Name' }, /SUITE_PROJECT must match/],
    [{ SUITE_APP_PORT: '99999' }, /SUITE_APP_PORT must be a port/],
    [{ SUITE_WORKERS: '0' }, /SUITE_WORKERS must be at least 1/],
    [{ SUITE_NICE: '25' }, /SUITE_NICE must be 0\.\.19/],
    [{ SUITE_CPUS: 'two' }, /SUITE_CPUS must be a number/],
    [{ SUITE_HOST_LOCKS: 'other ../x' }, /SUITE_HOST_LOCKS entry '\.\.\/x'/],
    [{ SUITE_COMPOSE_FILES: 'missing.yml' }, /compose file not found/],
    [{ SUITE_COMPOSE_FILES: '../outside.yml' }, /must be a path inside the site root/],
    [{ SUITE_FIXTURE_FRESHEN_SQL: 'db/none.sql' }, /freshen SQL not found/],
    [{ SUITE_DB_USER: '' }, /needs SUITE_DB_USER and SUITE_DB_NAME/],
    [{ SUITE_HEALTH_PATH: 'health' }, /SUITE_HEALTH_PATH must start with \//],
  ])('refuses bad settings with exit 78: %j', (bad, msg) => {
    const sb = sandbox(bad as Record<string, string>);
    const r = run(sb);
    expect([r.code, r.out]).toEqual([78, expect.stringMatching(msg)]);
    expect(calls(sb)).toEqual([]);
  });

  it('refuses a missing suite.env with exit 78', () => {
    const sb = sandbox();
    const r = spawnSync('bash', [RUNNER], { env: runnerEnv(sb), encoding: 'utf8' });
    expect(r.status).toBe(78);
    expect(r.stderr).toMatch(/no suite.env/);
  });

  it('accepts * in SUITE_HOST_LOCKS as a value, never a glob of the current directory', () => {
    const sb = sandbox({ SUITE_HOST_LOCKS: '*' });
    const r = spawnSync('bash', [RUNNER, '--suite-env', sb.suiteEnv, '--print-config'], { env: runnerEnv(sb), encoding: 'utf8', cwd: KIT });
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('host_locks=*\n');
  });

  // The kit's own fixture names no site, yet must hold the registry sites' gate
  // locks by name: with '*' alone it waits only on lock files that already
  // exist, and after a reboot (/tmp is tmpfs) a site's lock file does not
  // exist until that site's gate first runs, so the two gates could overlap.
  const FIXTURE_ENV = path.join(KIT, 'fixture-site', 'tests', 'suite.env');
  const REPO = path.resolve(KIT, '..', '..');
  const FLEET = path.join(REPO, '_template', 'affiliate-site', 'scaffold', 'fleet_sites.py');

  it("the fixture's suite.env holds the registry sites' gate locks by name, plus '*', and both scenarios agree", () => {
    const names = execFileSync('python3', ['-I', FLEET, 'gate-projects', '--repo', REPO, '--except', 'kitfixture'], { encoding: 'utf8' }).trim();
    expect(names).not.toBe('');
    const sb = sandbox();
    for (const env of [FIXTURE_ENV, path.join(KIT, 'fixture-site', 'tests', 'suite.violations.env')]) {
      const r = spawnSync('bash', [RUNNER, '--suite-env', env, '--print-config'], { env: runnerEnv(sb), encoding: 'utf8', cwd: KIT });
      expect([env, r.status, r.stderr]).toEqual([env, 0, '']);
      expect(r.stdout).toContain(`host_locks=${names} *\n`);
    }
    // --print-config stops before any lock: none was created.
    expect(fs.readdirSync(sb.locks)).toEqual([]);
  });

  it("the fixture's suite.env falls back to '*' alone where the site registry cannot be read", () => {
    const sb = sandbox();
    // Four directories above the sandbox's tests/ there is no reusable-agents checkout.
    const above = path.resolve(path.dirname(sb.suiteEnv), '..', '..', '..', '..');
    expect(fs.existsSync(path.join(above, '_template', 'affiliate-site', 'scaffold', 'fleet_sites.py'))).toBe(false);
    fs.writeFileSync(sb.suiteEnv, fs.readFileSync(FIXTURE_ENV, 'utf8'));
    const r = spawnSync('bash', [RUNNER, '--suite-env', sb.suiteEnv, '--print-config'], { env: runnerEnv(sb), encoding: 'utf8' });
    expect([r.status, r.stderr]).toEqual([0, '']);
    expect(r.stdout).toContain('host_locks=*\n');
    // So does a host without python3 (only the fakes and bash on PATH).
    const bin = path.join(sb.root, 'bin');
    fs.mkdirSync(bin);
    for (const tool of ['bash', 'dirname', 'nice', 'basename', 'cat', 'sed', 'tr', 'mkdir', 'flock', 'git']) {
      const at = spawnSync('bash', ['-c', `command -v ${tool}`], { encoding: 'utf8' }).stdout.trim();
      if (at) fs.symlinkSync(at, path.join(bin, tool));
    }
    const noPython = spawnSync('bash', [RUNNER, '--suite-env', FIXTURE_ENV, '--print-config'], {
      env: { ...runnerEnv(sb), PATH: `${FAKES}:${bin}` }, encoding: 'utf8', cwd: KIT,
    });
    expect([noPython.status, noPython.stderr]).toEqual([0, '']);
    expect(noPython.stdout).toContain('host_locks=*\n');
  });

  it('maps the site into GATE_SOURCE_REPO by its path inside the repository', () => {
    const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-suite-repo-'));
    sandboxes.push(repo);
    execFileSync('git', ['init', '-q', repo]);
    const sub = path.join(repo, 'sites', 'one');
    fs.mkdirSync(path.join(sub, 'tests'), { recursive: true });
    fs.writeFileSync(path.join(sub, 'docker-compose.yml'), '');
    fs.writeFileSync(path.join(sub, 'docker-compose.test.yml'), '');
    fs.writeFileSync(path.join(sub, 'tests', 'suite.env'), "SUITE_PROJECT='one'\nSUITE_APP_PORT='10040'\n");
    const sb = sandbox();
    const r = spawnSync('bash', [RUNNER, '--suite-env', path.join(sub, 'tests', 'suite.env'), '--print-config'], {
      env: runnerEnv(sb, { GATE_SOURCE_REPO: '/live/checkout' }), encoding: 'utf8',
    });
    expect(r.stdout).toContain('source_site_root=/live/checkout/sites/one\n');
  });
});

describe('a gate run', () => {
  it('builds, starts, caps, waits, freshens and runs Playwright niced, then removes the app container', () => {
    const sb = sandbox();
    const r = run(sb, ['--grep', 'family'], { GATE_ARTIFACT_DIR: sb.artifacts });
    expect(r.code).toBe(0);
    const log = calls(sb);
    const at = (re: RegExp): number => log.findIndex((l) => re.test(l));
    const last = (re: RegExp): number => log.map((l) => re.test(l)).lastIndexOf(true);
    expect(at(/^docker compose .* build backend-test$/)).toBeGreaterThan(-1);
    expect(at(/^docker compose .* up -d --no-deps --force-recreate backend-test$/)).toBeGreaterThan(at(/ build backend-test$/));
    expect(at(/^docker update --cpus 2 fx_backend_test$/)).toBeGreaterThan(at(/--no-deps/));
    expect(at(/^curl -fs --max-time 3 -o \/dev\/null http:\/\/localhost:10031\/health$/)).toBeGreaterThan(at(/^docker update/));
    expect(at(/^docker exec -i fx_postgres psql -X -q -v ON_ERROR_STOP=1 -U fx -d fx_db$/)).toBeGreaterThan(at(/^curl/));
    expect(at(/^npx --no-install playwright test --workers=4 --grep family$/)).toBeGreaterThan(at(/^docker exec/));
    // Removed before it is started (a leftover) and again after the run.
    expect(at(/^docker rm -f fx_backend_test$/)).toBeLessThan(at(/--no-deps/));
    expect(last(/^docker rm -f fx_backend_test$/)).toBeGreaterThan(at(/^npx/));
    expect(fs.readFileSync(path.join(sb.fake, 'freshen.in'), 'utf8')).toBe('UPDATE t SET fetched_at = now();\n');
    const env = npxEnv(sb);
    expect(env.cwd).toBe(fs.realpathSync(path.join(sb.site, 'tests')));
    expect(env.TEST_URL).toBe('http://localhost:10031');
    expect(env.KIT_GATE_DB_URL).toBe('postgresql://fx:fxpass@127.0.0.1:5433/fx_db');
    expect(env.SUITE_PROJECT).toBe('fx');
    expect(Number(env.nice)).toBe(Math.min(19, baseNice + 10));
    expect(env['lock-held']).toBe('local-suite-fx.lock');
    expect(fs.readFileSync(path.join(sb.artifacts, 'image-under-test.txt'), 'utf8')).toBe('sha256:fakeimage0123456789abcdef\n');
    expect(fs.existsSync(path.join(sb.artifacts, 'container.log'))).toBe(false);
  });

  it('passes Playwright\'s exit code through and keeps the container log of a red run', () => {
    const sb = sandbox();
    const r = run(sb, [], { FAKE_NPX_RC: '1', GATE_ARTIFACT_DIR: sb.artifacts });
    expect(r.code).toBe(1);
    expect(r.out).toContain('[suite] playwright exit=1');
    expect(fs.readFileSync(path.join(sb.artifacts, 'container.log'), 'utf8')).toContain('fake container log line');
  });

  it('skips nice and the CPU cap when they are off, and leaves the app up with KEEP_UP=1', () => {
    const sb = sandbox({ SUITE_NICE: '0', SUITE_CPUS: '0' });
    const r = run(sb, [], { KEEP_UP: '1' });
    expect(r.code).toBe(0);
    expect(Number(npxEnv(sb).nice)).toBe(baseNice);
    expect(calls(sb).some((l) => l.startsWith('docker update'))).toBe(false);
    expect(calls(sb).some((l) => l === 'docker rm -f fx_backend_test' && calls(sb).indexOf(l) > calls(sb).findIndex((x) => x.startsWith('npx')))).toBe(false);
    expect(r.out).toContain('KEEP_UP=1: leaving http://localhost:10031 running');
  });

  it('starts a stopped fixture DB from the first compose file', () => {
    const sb = sandbox();
    const r = run(sb, [], { FAKE_DB_RUNNING: 'false' });
    expect(r.code).toBe(0);
    expect(calls(sb)).toContain(`docker compose -p fx --project-directory ${fs.realpathSync(sb.site)} -f ${fs.realpathSync(sb.site)}/docker-compose.yml up -d postgres`);
  });
});

describe('exit codes', () => {
  it('2: the fixture DB cannot be started', () => {
    const sb = sandbox();
    expect(run(sb, [], { FAKE_DB_RUNNING: 'false', FAKE_DB_UP_RC: '1' }).code).toBe(2);
  });

  it('2: the fixture DB container exited (fails at once, with its log)', () => {
    const sb = sandbox();
    const r = run(sb, [], { FAKE_DB_RUNNING: 'false', FAKE_DB_UP_RC: '0', FAKE_DB_STATUS: 'exited' });
    expect(r.code).toBe(2);
    expect(r.out).toMatch(/fx_postgres exited \(code 1\)/);
  });

  it('2: the fixture DB never turns healthy', () => {
    const sb = sandbox({ SUITE_DB_WAIT_S: '2' });
    const r = run(sb, [], { FAKE_DB_HEALTH: 'starting' });
    expect(r.code).toBe(2);
    expect(r.out).toMatch(/not healthy after/);
  });

  it('8: the freshen SQL fails (it runs after the commit\'s migrations, so it is not "the fixture DB is not up")', () => {
    const sb = sandbox();
    const r = run(sb, [], { FAKE_PSQL_RC: '3' });
    expect(r.code).toBe(8);
    expect(r.out).toMatch(/the freshen SQL failed on fx_postgres\/fx_db/);
    expect(calls(sb).some((l) => l.startsWith('npx'))).toBe(false);
    // The app container it started is still removed.
    expect(calls(sb).filter((l) => l === 'docker rm -f fx_backend_test')).toHaveLength(2);
  });

  it('3: the image does not build', () => {
    const sb = sandbox();
    const r = run(sb, [], { FAKE_BUILD_RC: '1' });
    expect(r.code).toBe(3);
    expect(r.out).toContain('BUILD FAILED');
  });

  it('4: the container does not start', () => {
    const sb = sandbox();
    expect(run(sb, [], { FAKE_UP_RC: '1' }).code).toBe(4);
  });

  it('5: the container exits before it is healthy', () => {
    const sb = sandbox();
    const r = run(sb, [], { FAKE_HEALTH: 'down', FAKE_APP_RUNNING: 'false' });
    expect(r.code).toBe(5);
    expect(r.out).toContain('container exited');
    expect(r.out).toContain('fake container log line');
    expect(calls(sb).filter((l) => l === 'docker rm -f fx_backend_test')).toHaveLength(2);
  });

  it('6: the container never becomes healthy', () => {
    const sb = sandbox({ SUITE_HEALTH_TIMEOUT_S: '1', SUITE_HEALTH_INTERVAL_S: '1' });
    const r = run(sb, [], { FAKE_HEALTH: 'down' });
    expect(r.code).toBe(6);
    expect(r.out).toContain('never became healthy');
  });

  it('6 is not reported when health arrives on a later try', () => {
    const sb = sandbox();
    expect(run(sb, [], { FAKE_HEALTH_AFTER: '3' }).code).toBe(0);
  });
});

describe('locks', () => {
  it('7: another run of the same project holds its lock past SUITE_LOCK_WAIT_S', async () => {
    const sb = sandbox({ SUITE_LOCK_WAIT_S: '1' });
    await holdLock(path.join(sb.locks, 'local-suite-fx.lock'));
    const r = run(sb);
    expect(r.code).toBe(7);
    expect(r.out).toMatch(/another run holds .*local-suite-fx\.lock/);
    expect(r.out).toMatch(/gave up waiting for the suite lock/);
    expect(calls(sb)).toEqual([]);
  });

  it('7: a named host lock (another site\'s gate) is held', async () => {
    const sb = sandbox({ SUITE_LOCK_WAIT_S: '1', SUITE_HOST_LOCKS: 'other' });
    await holdLock(path.join(sb.locks, 'local-suite-other.lock'));
    expect(run(sb).code).toBe(7);
  });

  it('7: with *, any other gate lock present on the host is waited for', async () => {
    const sb = sandbox({ SUITE_LOCK_WAIT_S: '1', SUITE_HOST_LOCKS: '*' });
    await holdLock(path.join(sb.locks, 'local-suite-someone.lock'));
    expect(run(sb).code).toBe(7);
  });

  it('holds its own and the host locks for the whole run, and releases them after', () => {
    const sb = sandbox({ SUITE_HOST_LOCKS: '* named' });
    fs.writeFileSync(path.join(sb.locks, 'local-suite-present.lock'), '');
    expect(run(sb).code).toBe(0);
    const env = npxEnv(sb);
    expect(env['lock-held']?.split(',').sort()).toEqual(['local-suite-fx.lock', 'local-suite-named.lock', 'local-suite-present.lock']);
    expect(env['lock-free']).toBeUndefined();
    for (const f of fs.readdirSync(sb.locks)) {
      expect(spawnSync('flock', ['-n', path.join(sb.locks, f), 'true']).status).toBe(0);
    }
  });

  it('holds no lock while it waits for a busy one, so it never blocks a gate it does not conflict with', async () => {
    const sb = sandbox({ SUITE_LOCK_WAIT_S: '30', SUITE_HOST_LOCKS: 'aaa zzz' });
    const holder = await holdLock(path.join(sb.locks, 'local-suite-zzz.lock'));
    const c = spawn('bash', [RUNNER, '--suite-env', sb.suiteEnv], { env: runnerEnv(sb) });
    let out = '';
    c.stdout.on('data', (d: Buffer) => { out += d.toString(); });
    c.stderr.on('data', (d: Buffer) => { out += d.toString(); });
    const closed = new Promise<number | null>((resolve) => c.on('close', resolve));
    for (let i = 0; i < 200 && !/another run holds/.test(out); i++) await new Promise((r) => setTimeout(r, 50));
    expect(out).toMatch(/another run holds .*local-suite-zzz\.lock; waiting .*holding no lock meanwhile/);
    // Sorted before the busy one, so the old hold-and-wait would have kept them.
    for (const f of ['local-suite-aaa.lock', 'local-suite-fx.lock']) {
      expect([f, spawnSync('flock', ['-n', path.join(sb.locks, f), 'true']).status]).toEqual([f, 0]);
    }
    release(holder);
    expect(await closed).toBe(0);
    expect(npxEnv(sb)['lock-held']?.split(',').sort()).toEqual(['local-suite-aaa.lock', 'local-suite-fx.lock', 'local-suite-zzz.lock']);
  }, 30_000);

  it('with *, takes a gate lock that appeared while it waited', async () => {
    const sb = sandbox({ SUITE_LOCK_WAIT_S: '30', SUITE_HOST_LOCKS: '*' });
    const holder = await holdLock(path.join(sb.locks, 'local-suite-busy.lock'));
    const c = spawn('bash', [RUNNER, '--suite-env', sb.suiteEnv], { env: runnerEnv(sb) });
    let out = '';
    c.stdout.on('data', (d: Buffer) => { out += d.toString(); });
    const closed = new Promise<number | null>((resolve) => c.on('close', resolve));
    for (let i = 0; i < 200 && !/another run holds/.test(out); i++) await new Promise((r) => setTimeout(r, 50));
    fs.writeFileSync(path.join(sb.locks, 'local-suite-late.lock'), '');
    release(holder);
    expect(await closed).toBe(0);
    expect(npxEnv(sb)['lock-held']?.split(',').sort()).toEqual(['local-suite-busy.lock', 'local-suite-fx.lock', 'local-suite-late.lock']);
  }, 30_000);

  it('waits for a lock that is released while it waits', async () => {
    const sb = sandbox({ SUITE_LOCK_WAIT_S: '30' });
    const holder = await holdLock(path.join(sb.locks, 'local-suite-fx.lock'));
    setTimeout(() => release(holder), 1500);
    const r = await new Promise<{ code: number | null; out: string }>((resolve) => {
      const c = spawn('bash', [RUNNER, '--suite-env', sb.suiteEnv], { env: runnerEnv(sb) });
      let out = '';
      c.stdout.on('data', (d: Buffer) => { out += d.toString(); });
      c.stderr.on('data', (d: Buffer) => { out += d.toString(); });
      c.on('close', (code) => resolve({ code, out }));
    });
    expect(r.code).toBe(0);
    expect(r.out).toMatch(/another run holds/);
  }, 30_000);
});

describe('signals', () => {
  it('143: TERM during Playwright stops it, removes the app container and exits 143', async () => {
    const sb = sandbox();
    const child = spawn('bash', [RUNNER, '--suite-env', sb.suiteEnv], { env: runnerEnv(sb, { FAKE_NPX_SLEEP: '30' }) });
    let out = '';
    child.stdout.on('data', (d: Buffer) => { out += d.toString(); });
    const pidFile = path.join(sb.fake, 'npx.pid');
    for (let i = 0; i < 200 && !fs.existsSync(pidFile); i++) await new Promise((r) => setTimeout(r, 50));
    expect(fs.existsSync(pidFile)).toBe(true);
    const npxPid = Number(fs.readFileSync(pidFile, 'utf8'));
    child.kill('SIGTERM');
    const code = await new Promise<number | null>((resolve) => child.on('close', resolve));
    expect(code).toBe(143);
    expect(out).toContain('[suite] stopping backend-test');
    expect(calls(sb)).toContain('docker rm -f fx_backend_test');
    let alive = true;
    try { process.kill(npxPid, 0); } catch { alive = false; }
    expect(alive).toBe(false);
  });

  it('143: TERM during the build stops the build', async () => {
    const sb = sandbox();
    const child = spawn('bash', [RUNNER, '--suite-env', sb.suiteEnv], { env: runnerEnv(sb, { FAKE_BUILD_SLEEP: '30' }) });
    for (let i = 0; i < 200 && !calls(sb).some((l) => / build backend-test$/.test(l)); i++) await new Promise((r) => setTimeout(r, 50));
    const t0 = Date.now();
    child.kill('SIGTERM');
    const code = await new Promise<number | null>((resolve) => child.on('close', resolve));
    expect(code).toBe(143);
    expect(Date.now() - t0).toBeLessThan(5000);
  });
});
