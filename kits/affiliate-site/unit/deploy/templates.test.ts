/**
 * The Dockerfile template, the pre-deploy gate wrapper, and shellcheck over
 * every deploy script.
 */
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { BASH, DEPLOY, makeSandbox, removeSandbox, runScript, which, type Sandbox } from './harness';

jest.setTimeout(60_000);

const KIT_VERSION = fs.readFileSync(path.join(DEPLOY, '..', 'VERSION'), 'utf8').trim();

let sb: Sandbox;
beforeAll(() => {
  sb = makeSandbox({ real: ['git'] });
});
afterAll(() => removeSandbox(sb));

describe('Dockerfile.azure.tmpl', () => {
  let df: string;
  beforeAll(() => {
    df = fs.readFileSync(path.join(sb.fx, 'site/Dockerfile.azure'), 'utf8');
  });

  it('renders completely, starting with the drift marker', () => {
    expect(df).not.toMatch(/\{\{|\}\}/);
    expect(df.split('\n')[0]).toBe(`# rendered-from: affiliate-kit/deploy/Dockerfile.azure.tmpl (kit ${KIT_VERSION})`);
  });

  it('is SSR only: deps, build and a runtime stage, no SPA/frontend stage', () => {
    const stages = [...df.matchAll(/^FROM (\S+) AS (\S+)$/gm)].map((m) => [m[1], m[2]]);
    expect(stages).toEqual([['${NODE_IMAGE}', 'deps'], ['${NODE_IMAGE}', 'build'], ['${NODE_IMAGE}', 'production']]);
    expect(df).toContain('ARG NODE_IMAGE=node:22-alpine');
    expect(df).not.toMatch(/frontend|vite|webpack|react/i);
  });

  it('installs production deps only, builds with npm, copies the kit and the extra paths', () => {
    expect(df).toContain('RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force && mkdir -p node_modules');
    expect(df).toContain('RUN npm run build \\\n && test -f dist/src/index.js');
    expect(df).toContain('COPY vendor/affiliate-kit ./vendor/affiliate-kit');
    expect(df).toContain('COPY db ./db\nCOPY content ./content');
    expect(df).toContain('COPY --from=build /app/vendor/affiliate-kit ./vendor/affiliate-kit');
  });

  it('makes everything the runtime stage copies from the build stage readable by the server, never writable', () => {
    // The build context keeps the checkout's modes; a 0640 checkout would leave
    // these unreadable to USER node (reproduced 2026-11-17).
    const chmods = [...df.matchAll(/^RUN chmod -R (\S+) (.+)$/gm)];
    expect(chmods).toHaveLength(1);
    const [, mode, list] = chmods[0] as RegExpMatchArray;
    expect(mode).toBe('a+rX,go-w');
    expect(mode).not.toMatch(/[+=][rwxXst]*w/); // it adds no write bit to anyone
    const covered = new Set((list as string).split(' '));
    const copied = [...df.matchAll(/^COPY --from=build \/app\/(\S+) /gm)].map((m) => m[1] as string);
    expect(copied).toEqual(['package.json', 'dist', 'site.json', 'vendor/affiliate-kit', 'db', 'content']);
    expect(copied.filter((p) => !covered.has(p))).toEqual([]);
    // In the build stage, after the build, so it also covers what the build wrote into dist.
    const at = df.indexOf(chmods[0]![0]);
    expect(at).toBeGreaterThan(df.indexOf('RUN npm run build'));
    expect(at).toBeLessThan(df.indexOf('FROM ${NODE_IMAGE} AS production'));
  });

  it('runs as the unprivileged node user on TARGET_PORT with a /health check and dumb-init', () => {
    expect(df).toMatch(/^USER node$/m);
    expect(df).toMatch(/^EXPOSE 8080$/m);
    expect(df).toContain('PORT=8080');
    expect(df).toContain('CMD curl -fsS "http://127.0.0.1:8080/health" > /dev/null || exit 1');
    expect(df).toContain('ENTRYPOINT ["dumb-init", "--"]');
    expect(df).toContain('CMD ["node", "dist/src/index.js"]');
  });

  it('bakes no secret into the image', () => {
    for (const line of df.split('\n').filter((l) => /^\s*(ARG|ENV)\s/.test(l))) {
      expect(line).not.toMatch(/DATABASE_URL|PASSWORD|SECRET|TOKEN|API_KEY/i);
    }
  });

  it('render-dockerfile.sh --check passes on the render and fails (exit 3) after a hand edit', () => {
    expect(runScript(sb, 'render-dockerfile.sh', ['--env', sb.envFile, '--check']).status).toBe(0);
    const f = path.join(sb.fx, 'site/Dockerfile.azure');
    fs.writeFileSync(f, df.replace('USER node', 'USER root'));
    try {
      const r = runScript(sb, 'render-dockerfile.sh', ['--env', sb.envFile, '--check']);
      expect(r.status).toBe(3);
      expect(r.stderr).toContain('-USER root');
    } finally {
      fs.writeFileSync(f, df);
    }
  });

  it('--dry-run with --write or --out prints the rendering and writes nothing', () => {
    const f = path.join(sb.fx, 'port.env');
    fs.writeFileSync(f, fs.readFileSync(sb.envFile, 'utf8').replace('TARGET_PORT=8080', 'TARGET_PORT=3000'));
    const target = path.join(sb.fx, 'site/Dockerfile.azure');
    const before = fs.readFileSync(target, 'utf8');
    const r = runScript(sb, 'render-dockerfile.sh', ['--env', f, '--dry-run', '--write']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain(`[dry-run] (file ${target}, differs from this rendering; not written)`);
    expect(r.stdout).toMatch(/^EXPOSE 3000$/m);
    expect(fs.readFileSync(target, 'utf8')).toBe(before);
    const same = runScript(sb, 'render-dockerfile.sh', ['--env', sb.envFile, '--dry-run', '--write']);
    expect(same.stdout).toContain('already matches this rendering; not written');
    const out = path.join(sb.fx, 'never-written.Dockerfile');
    const o = runScript(sb, 'render-dockerfile.sh', ['--env', sb.envFile, '--dry-run', '--out', out]);
    expect(o.stdout).toContain('does not exist yet; not written');
    expect(fs.existsSync(out)).toBe(false);
  });

  it('follows deploy.env (port, entry, health path, node image)', () => {
    const f = path.join(sb.fx, 'other.env');
    fs.writeFileSync(f, fs.readFileSync(sb.envFile, 'utf8')
      .replace('TARGET_PORT=8080', 'TARGET_PORT=3000').replace('SERVER_ENTRY=dist/src/index.js', 'SERVER_ENTRY=dist/server.js')
      .replace('HEALTH_PATH=/health', 'HEALTH_PATH=/healthz').replace('NODE_IMAGE=node:22-alpine', 'NODE_IMAGE=node:22-bookworm-slim')
      .replace('DOCKER_EXTRA_PATHS="db content"', 'DOCKER_EXTRA_PATHS='));
    const r = runScript(sb, 'render-dockerfile.sh', ['--env', f]);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('ARG NODE_IMAGE=node:22-bookworm-slim');
    expect(r.stdout).toMatch(/^EXPOSE 3000$/m);
    expect(r.stdout).toContain('http://127.0.0.1:3000/healthz');
    expect(r.stdout).toContain('CMD ["node", "dist/server.js"]');
    expect(r.stdout).toContain('# (none: DOCKER_EXTRA_PATHS is empty)');
    expect(r.stdout).not.toContain('COPY db');
  });
});

describe('predeploy-gate.sh', () => {
  function gate(env: Record<string, string> = {}, args: string[] = ['--', '--workers=4']) {
    return runScript(sb, 'predeploy-gate.sh', ['--site-root', path.join(sb.fx, 'site'), '--suite', 'tests/run-local-suite.sh', ...args], env);
  }

  it('runs the suite from its own directory with the gate arguments and passes', () => {
    const r = gate();
    expect(r.status).toBe(0);
    expect(r.stdout).toContain(`fake suite ran in ${path.join(sb.fx, 'site/tests')} with: --workers=4`);
    expect(r.stdout).toContain('[gate] passed');
  });

  it('fails when the suite fails', () => {
    const r = gate({ FAKE_SUITE_EXIT: '3' });
    expect(r.status).toBe(1);
    expect(r.stdout).toContain('[gate] FAILED (exit 3)');
  });

  it('--dry-run prints the suite command and runs nothing', () => {
    const r = gate({}, ['--dry-run', '--', '--workers=4']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain(`[dry-run] [gate] would run: (cd ${path.join(sb.fx, 'site/tests')} && bash run-local-suite.sh --workers=4)`);
    expect(r.stdout).not.toContain('fake suite ran');
    const missing = runScript(sb, 'predeploy-gate.sh', ['--site-root', path.join(sb.fx, 'site'), '--suite', 'tests/nope.sh', '--dry-run']);
    expect(missing.status).toBe(1);
    expect(missing.stderr).toContain('Untested code does not ship');
  });

  it('fails when there is no suite (untested code does not ship)', () => {
    const r = runScript(sb, 'predeploy-gate.sh', ['--site-root', path.join(sb.fx, 'site'), '--suite', 'tests/nope.sh']);
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('Untested code does not ship');
  });

  it('fails when the working tree changes while the suite runs', () => {
    const git = which('git');
    if (!git) return;
    const site = path.join(sb.fx, 'site');
    const g = (...a: string[]) => spawnSync(git, ['-C', site, ...a], { encoding: 'utf8', env: { ...process.env, GIT_CONFIG_NOSYSTEM: '1' } });
    g('init', '-q');
    g('-c', 'user.email=t@example.test', '-c', 'user.name=t', 'add', '-A');
    g('-c', 'user.email=t@example.test', '-c', 'user.name=t', 'commit', '-qm', 'fixture');
    const suite = path.join(site, 'tests/run-local-suite.sh');
    const siteJson = path.join(site, 'site.json');
    const orig = fs.readFileSync(suite, 'utf8');
    const origJson = fs.readFileSync(siteJson, 'utf8');
    fs.writeFileSync(suite, `${orig.replace('exit "${FAKE_SUITE_EXIT:-0}"', '')}echo changed >> "$(pwd)/../site.json"\nexit 0\n`);
    g('-c', 'user.email=t@example.test', '-c', 'user.name=t', 'commit', '-qam', 'mutating suite');
    try {
      const clean = gate({}, ['--', '--workers=4']);
      expect(clean.status).toBe(1);
      expect(clean.stdout).toContain('the working tree changed while the gate ran');
    } finally {
      fs.writeFileSync(suite, orig);
      fs.writeFileSync(siteJson, origJson);
      fs.rmSync(path.join(site, '.git'), { recursive: true, force: true });
    }
  });
});

describe('shellcheck', () => {
  const shellcheck = process.env.SHELLCHECK || which('shellcheck');
  const scripts = ['lib/common.sh', 'azure/azure.sh', 'azure/provision.sh', 'azure/deploy.sh', 'azure/teardown.sh', 'aws/aws.sh',
    'aws/provision.sh', 'aws/deploy.sh', 'aws/teardown.sh', 'predeploy-gate.sh', 'render-dockerfile.sh'];

  (shellcheck ? it : it.skip)('finds nothing in the deploy scripts', () => {
    const r = spawnSync(shellcheck!, ['-x', '-P', 'SCRIPTDIR', ...scripts], { cwd: DEPLOY, encoding: 'utf8' });
    expect(r.stdout + r.stderr).toBe('');
    expect(r.status).toBe(0);
  });

  it('every script is valid bash (bash -n)', () => {
    for (const s of scripts) {
      const r = spawnSync(BASH, ['-n', path.join(DEPLOY, s)], { encoding: 'utf8' });
      expect({ s, status: r.status, err: r.stderr }).toEqual({ s, status: 0, err: '' });
    }
  });

  it('deploytool.py parses (without writing bytecode into the kit)', () => {
    const r = spawnSync('python3', ['-I', '-c', 'import ast, sys; ast.parse(open(sys.argv[1]).read())', path.join(DEPLOY, 'lib/deploytool.py')],
      { encoding: 'utf8' });
    expect(r.status).toBe(0);
  });
});
