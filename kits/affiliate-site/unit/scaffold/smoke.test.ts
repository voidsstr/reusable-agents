import * as crypto from 'crypto';
import * as fs from 'fs';
import * as net from 'net';
import * as os from 'os';
import * as path from 'path';
import { REPO, createSite, describeRun, readJson, registryGateProjects, run } from './helpers';

/**
 * The scaffold smoke (build item K7): a generated site installs, type-checks,
 * builds, and passes the kit's deploy gate on its own fixture.
 *
 *   KIT_SCAFFOLD_SMOKE=1 npx jest unit/scaffold/smoke.test.ts
 *
 * Opt-in: it needs docker, the npm registry (or a warm npm cache), the
 * host's Playwright Chromium and several minutes. It generates the site
 * under KIT_SCAFFOLD_SMOKE_DIR (default: kit-scaffold-smoke in the OS temp
 * directory), runs `npm ci`, `npm run typecheck`, `npm run build`,
 * `npm test`, then tests/run-local-suite.sh: the vendored-kit checks (its
 * manifest, then the anchor against this checkout, where the kit is not
 * committed yet: the working tree matches, or the gate warns when another
 * change landed since the sync), the
 * production image built from the site's rendered Dockerfile.azure, its
 * fixture Postgres, the kit's specs and the site's own. The gate never
 * overlaps another gate on this host: it holds the registry sites' gate locks
 * and any other present (SUITE_HOST_LOCKS), so it waits for one already
 * running, and one that starts meanwhile waits for it. Ports are free ones
 * above 55000.
 *
 * Afterwards it removes everything it made: the fixture Postgres container,
 * its volume and network, the image under test, its gate lock file and the
 * site directory.
 */

const ENABLED = process.env.KIT_SCAFFOLD_SMOKE === '1';
const SMOKE_ROOT = path.resolve(process.env.KIT_SCAFFOLD_SMOKE_DIR || path.join(os.tmpdir(), 'kit-scaffold-smoke'));
const LOCK_DIR = process.env.AGENT_LOCK_DIR || '/tmp/agent-framework-locks';
/**
 * Gate tests that judge a product catalog the scaffold does not have yet: the
 * recall check (skipped with the scaffold's recall waiver, its reason printed)
 * and the product-page specs (not configured).
 */
const CATALOG_ONLY = [/a recalled SKU has no buy button/, /in-stock product page/, /unbuyable product page/];

function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', () => resolve(false));
    srv.listen(port, '127.0.0.1', () => srv.close(() => resolve(true)));
  });
}

async function freePorts(n: number): Promise<number[]> {
  const out: number[] = [];
  while (out.length < n) {
    const p = 55_800 + crypto.randomInt(0, 4000);
    if (!out.includes(p) && (await portFree(p))) out.push(p);
  }
  return out;
}

interface ReportTest { title: string; status: string; project: string }

function reportTests(file: string): ReportTest[] {
  const out: ReportTest[] = [];
  type Suite = { specs?: Array<{ title: string; tests?: Array<{ status: string; projectName: string }> }>; suites?: Suite[] };
  const walk = (s: Suite): void => {
    for (const spec of s.specs ?? []) for (const t of spec.tests ?? []) out.push({ title: spec.title, status: t.status, project: t.projectName });
    for (const c of s.suites ?? []) walk(c);
  };
  for (const s of readJson<{ suites?: Suite[] }>(file).suites ?? []) walk(s);
  return out;
}

(ENABLED ? describe : describe.skip)('scaffold smoke: a generated site passes the kit gate on its fixture', () => {
  let root = '';
  let site = '';
  let slug = '';

  afterAll(() => {
    if (!site) return;
    run('docker', ['compose', '-p', slug, '--project-directory', site, '-f', path.join(site, 'docker-compose.yml'),
      '-f', path.join(site, 'docker-compose.test.yml'), 'down', '--volumes', '--remove-orphans'], { timeoutMs: 180_000 });
    run('docker', ['rmi', '-f', `${slug}-backend-test:latest`], { timeoutMs: 120_000 });
    fs.rmSync(path.join(LOCK_DIR, `local-suite-${slug}.lock`), { force: true });
    if (root.startsWith(`${SMOKE_ROOT}/`)) fs.rmSync(root, { recursive: true, force: true });
  }, 300_000);

  it('generates, installs, type-checks, builds, unit-tests and passes the gate', async () => {
    fs.mkdirSync(SMOKE_ROOT, { recursive: true });
    root = fs.mkdtempSync(path.join(SMOKE_ROOT, 'k7-smoke-'));
    slug = `k7smoke-${crypto.randomBytes(3).toString('hex')}`;
    const [gatePort, dbPort] = await freePorts(2);
    const created = createSite(root, {
      slug,
      extra: ['--brand', 'Smoke Test Picks', '--niche', 'widgets', '--gate-port', String(gatePort), '--fixture-db-port', String(dbPort),
        '--content-rule-sets', 'electrical,medical'],
    });
    expect(describeRun(created.result)).toMatch(/^exit 0/);
    site = created.dir;

    for (const [cmd, args] of [
      ['npm', ['ci', '--no-audit', '--no-fund']],
      ['npm', ['run', '--silent', 'typecheck']],
      ['npm', ['run', '--silent', 'build']],
      ['npm', ['test', '--silent', '--', '--ci']],
    ] as Array<[string, string[]]>) {
      const r = run(cmd, args, { cwd: site, timeoutMs: 600_000, env: { KIT_GATE_FRAMEWORK_DIR: REPO } });
      expect(`${cmd} ${args.join(' ')}: ${describeRun(r)}`).toMatch(/: exit 0/);
    }

    const artifacts = path.join(root, 'gate-artifacts');
    const report = path.join(artifacts, 'report.json');
    const gate = run('bash', ['run-local-suite.sh', '--reporter=line,json'], {
      cwd: path.join(site, 'tests'),
      timeoutMs: 1_800_000,
      env: { KIT_GATE_FRAMEWORK_DIR: REPO, GATE_ARTIFACT_DIR: artifacts, PLAYWRIGHT_JSON_OUTPUT_FILE: report },
    });
    expect(describeRun(gate)).toMatch(/^exit 0/);
    expect(gate.stdout).toMatch(/vendor\/affiliate-kit: kit \S+, \d+ files match MANIFEST\.sha256/);
    // The anchor ran against this checkout (step 2 of the shim).
    expect(`${gate.stdout}\n${gate.stderr}`).toMatch(/identical to (kits\/affiliate-site at commit|the kit working tree)|NOT anchored to reusable-agents: nothing in /);
    // It held its own lock and every registry site's gate lock (created when missing), plus any other present.
    const held = (/\[suite\] holding (.*)/.exec(gate.stdout)?.[1] ?? '').trim().split(/\s+/);
    for (const p of [...registryGateProjects(slug), slug]) expect(held).toContain(`local-suite-${p}.lock`);
    const tests = reportTests(report);
    const count = (status: string): number => tests.filter((t) => t.status === status).length;
    // eslint-disable-next-line no-console
    console.log(`scaffold smoke gate: ${count('expected')} passed, ${count('unexpected')} failed, ${count('flaky')} flaky, `
      + `${count('skipped')} skipped (${tests.filter((t) => t.status === 'skipped').map((t) => t.title.replace(/^kit-compliance: /, '')).join('; ')})`);
    expect(tests.filter((t) => t.status === 'unexpected' || t.status === 'flaky')).toEqual([]);
    // Every skip is a catalog check the scaffold is not configured for; nothing else may skip.
    expect(tests.filter((t) => t.status === 'skipped' && !CATALOG_ONLY.some((re) => re.test(t.title)))).toEqual([]);
    expect(tests.filter((t) => t.project === 'kit' && t.status === 'expected').length).toBeGreaterThan(30);
    expect(tests.filter((t) => t.project === 'site' && t.status === 'expected').length).toBeGreaterThan(0);
    expect(tests.some((t) => /content policy scan/.test(t.title) && t.status === 'expected')).toBe(true);
  }, 2_400_000);
});
