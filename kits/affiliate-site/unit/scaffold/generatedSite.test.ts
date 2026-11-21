import * as fs from 'fs';
import * as path from 'path';
import {
  ANCHOR_ENV_CLEARED, KIT_NODE_MODULES, KIT_VERSION, TEST_WAIVER, cleanupTmp, createSite, describeRun, fakeFramework, gitIn, rewriteManifestLine,
  run, tmpDir,
} from './helpers';

/**
 * A generated site works as a site, offline: it type-checks (its own code and
 * the vendored kit, under the kit's compiler options), builds the server and
 * the kit's browser bundle, and passes its own unit tests (kit integrity, the
 * app's routes and pages, the deploy files, the gate config through the
 * kit's own validator), and Playwright loads its gate (every kit spec
 * validates the gate config at load). Its node_modules is the kit's (a
 * symlink): the site pins exactly the kit's dependencies, so this is the tree
 * `npm ci` would install. smoke.test.ts does the real install, the image
 * build and the kit gate. This site waives the content gate (the waiver
 * variant); create.test.ts covers the rule-set variant.
 *
 * Next to the site sits a stand-in reusable-agents checkout (a git repository
 * whose committed kits/affiliate-site is the site's vendored copy), where the
 * gate shim and the kit-integrity test find it by default, so both run the
 * anchored check against commits.
 */

const NPM_TIMEOUT = 240_000;

describe('a generated site, offline', () => {
  let tmp = '';
  let site = '';
  let framework = '';
  beforeAll(() => {
    tmp = tmpDir('generated');
    const { dir, result } = createSite(tmp, {
      slug: 'offlinepicks', extra: ['--brand', 'Offline Picks', '--niche', 'gadgets', '--content-policy-waived', TEST_WAIVER],
    });
    if (result.status !== 0) throw new Error(describeRun(result));
    site = dir;
    fs.symlinkSync(KIT_NODE_MODULES, path.join(site, 'node_modules'));
    framework = fakeFramework(tmp, path.join(site, 'vendor', 'affiliate-kit'));
  }, 120_000);
  afterAll(() => cleanupTmp(tmp));

  it('type-checks: the site, its tests and the vendored kit', () => {
    const r = run('npm', ['run', '--silent', 'typecheck'], { cwd: site, timeoutMs: NPM_TIMEOUT });
    expect(describeRun(r)).toMatch(/^exit 0/);
  }, NPM_TIMEOUT);

  it('builds the server entry, the migration command and the kit browser bundle', () => {
    const r = run('npm', ['run', '--silent', 'build'], { cwd: site, timeoutMs: NPM_TIMEOUT });
    expect(describeRun(r)).toMatch(/^exit 0/);
    for (const f of ['dist/src/index.js', 'dist/vendor/affiliate-kit/server/src/db/migrate.js', 'dist/client/kit-client.js']) {
      expect(fs.existsSync(path.join(site, f))).toBe(true);
    }
    // The migration command the deploy runs (deploy.env MIGRATE_COMMAND) starts and explains itself.
    const help = run('node', ['dist/vendor/affiliate-kit/server/src/db/migrate.js', '--help'], { cwd: site });
    expect(describeRun(help)).toMatch(/^exit 0/);
    expect(help.stdout).toContain('--site-dir');
    // Building writes nothing into the vendored kit.
    expect(fs.existsSync(path.join(site, 'vendor/affiliate-kit/client/dist'))).toBe(false);
  }, NPM_TIMEOUT);

  it('passes its own unit tests', () => {
    const r = run('node', [path.join(site, 'node_modules/jest/bin/jest.js'), '--ci', '--colors=false'], { cwd: site, env: ANCHOR_ENV_CLEARED, timeoutMs: NPM_TIMEOUT });
    expect(describeRun(r)).toMatch(/^exit 0/);
    expect(r.stderr).toMatch(/Tests:\s+\d+ passed, \d+ total/);
    expect(r.stderr).toContain('tests/unit/kit-integrity.test.ts');
    expect(r.stderr).toContain('tests/unit/gate-config.test.ts');
    expect(r.stderr).toContain('tests/unit/app.test.ts');
    expect(r.stderr).toContain('tests/unit/deploy-config.test.ts');
  }, NPM_TIMEOUT);

  it('Playwright loads its gate: the kit\'s specs accept the gate config, and the site\'s own specs are listed', () => {
    const r = run(path.join(site, 'node_modules/.bin/playwright'), ['test', '--list'],
      { cwd: path.join(site, 'tests'), env: { TEST_URL: 'http://127.0.0.1:9' }, timeoutMs: NPM_TIMEOUT });
    expect(describeRun(r)).toMatch(/^exit 0/);
    expect(r.stdout).toContain('[kit]');
    expect(r.stdout).toContain('[site]');
    expect(r.stdout).toMatch(/Total: \d+ tests in \d+ files/);
  }, NPM_TIMEOUT);

  it('its gate stops (exit 78) on an edited vendored kit, before building anything', () => {
    const file = path.join(site, 'vendor/affiliate-kit/server/src/links.ts');
    const original = fs.readFileSync(file);
    try {
      fs.appendFileSync(file, '\n// a local "fix"\n');
      const r = run('bash', [path.join(site, 'tests/run-local-suite.sh'), '--print-config'], { env: ANCHOR_ENV_CLEARED });
      expect(r.status).toBe(78);
      expect(r.stderr).toContain('modified: server/src/links.ts');
      expect(r.stderr).toContain('install/sync-site-kit.sh');
      expect(r.stdout).not.toContain('project=');
    } finally {
      fs.writeFileSync(file, original);
    }
    expect(run('bash', [path.join(site, 'tests/run-local-suite.sh'), '--print-config'], { env: ANCHOR_ENV_CLEARED }).status).toBe(0);
  });

  it('its gate anchors the vendored kit to reusable-agents: exit 78 on an edit whose manifest line was rewritten to match', () => {
    const shim = path.join(site, 'tests/run-local-suite.sh');
    const head = gitIn(framework, 'rev-parse', 'HEAD').stdout.trim();
    // Found next to the site, as in the fleet layout (no variable set).
    const ok = run('bash', [shim, '--print-config'], { env: ANCHOR_ENV_CLEARED });
    expect(describeRun(ok)).toMatch(/^exit 0/);
    expect(ok.stdout).toContain(`kit ${KIT_VERSION}, `);
    expect(ok.stdout).toContain(`identical to kits/affiliate-site at commit ${head.slice(0, 12)}`);
    expect(ok.stdout).toContain('project=offlinepicks');

    const file = path.join(site, 'vendor/affiliate-kit/server/src/links.ts');
    const manifest = path.join(site, 'vendor/affiliate-kit/MANIFEST.sha256');
    const original = fs.readFileSync(file);
    const originalManifest = fs.readFileSync(manifest);
    try {
      fs.writeFileSync(file, original.toString('utf8').replace(/sponsored nofollow noopener/g, 'noopener'));
      rewriteManifestLine(site, 'server/src/links.ts');
      const r = run('bash', [shim, '--print-config'], { env: ANCHOR_ENV_CLEARED });
      expect(r.status).toBe(78);
      expect(r.stdout).toContain('files match MANIFEST.sha256'); // step 1 is fooled ...
      expect(r.stderr).toContain('modified: server/src/links.ts'); // ... step 2 is not
      expect(r.stderr).toContain('is not the kit release it claims');
      expect(r.stdout).not.toContain('project=');
      // GATE_SOURCE_REPO (the deployer's live checkout) resolves the same sibling checkout.
      const viaSource = run('bash', [shim, '--print-config'], { env: { ...ANCHOR_ENV_CLEARED, GATE_SOURCE_REPO: site } });
      expect(viaSource.status).toBe(78);
    } finally {
      fs.writeFileSync(file, original);
      fs.writeFileSync(manifest, originalManifest);
    }
    expect(run('bash', [shim, '--print-config'], { env: ANCHOR_ENV_CLEARED }).status).toBe(0);
  });

  it('without a reusable-agents checkout its gate warns that the kit was not anchored; KIT_ANCHOR_REQUIRED=1 makes that exit 78', () => {
    const shim = path.join(site, 'tests/run-local-suite.sh');
    const nowhere = path.join(tmp, 'no-such-checkout');
    const warned = run('bash', [shim, '--print-config'], { env: { ...ANCHOR_ENV_CLEARED, KIT_GATE_FRAMEWORK_DIR: nowhere } });
    expect(describeRun(warned)).toMatch(/^exit 0/);
    expect(warned.stderr).toContain(`NOT anchored to reusable-agents: no reusable-agents checkout at ${nowhere}`);
    expect(warned.stderr).toContain('checked only against its own MANIFEST.sha256');
    expect(warned.stdout).toContain('project=offlinepicks');
    const strict = run('bash', [shim, '--print-config'], { env: { ...ANCHOR_ENV_CLEARED, KIT_GATE_FRAMEWORK_DIR: nowhere, KIT_ANCHOR_REQUIRED: '1' } });
    expect(strict.status).toBe(78);
    expect(strict.stderr).toContain('KIT_ANCHOR_REQUIRED=1');
    expect(strict.stdout).not.toContain('project=');
  });

  it('its kit-integrity test fails on an edit whose manifest line was rewritten, when the reusable-agents checkout is found', () => {
    const file = path.join(site, 'vendor/affiliate-kit/server/src/price.ts');
    const manifest = path.join(site, 'vendor/affiliate-kit/MANIFEST.sha256');
    const original = fs.readFileSync(file);
    const originalManifest = fs.readFileSync(manifest);
    const jest = (env: NodeJS.ProcessEnv = {}) => run('node', [path.join(site, 'node_modules/jest/bin/jest.js'), '--ci', '--colors=false',
      'tests/unit/kit-integrity.test.ts'], { cwd: site, env: { ...ANCHOR_ENV_CLEARED, ...env }, timeoutMs: NPM_TIMEOUT });
    try {
      fs.appendFileSync(file, '\n// a local "fix", manifest included\n');
      rewriteManifestLine(site, 'server/src/price.ts');
      const r = jest();
      expect(r.status).toBe(1);
      expect(r.stderr).toContain(`matches no copy of kit ${KIT_VERSION}`);
      expect(r.stderr).toContain('modified: server/src/price.ts');
      // Without the checkout it can only warn (or fail, when required).
      const nowhere = { KIT_GATE_FRAMEWORK_DIR: path.join(tmp, 'no-such-checkout') };
      const warned = jest(nowhere);
      expect(describeRun(warned)).toMatch(/^exit 0/);
      expect(`${warned.stdout}${warned.stderr}`).toContain('was NOT anchored to reusable-agents'); // jest prints console output on stdout
      expect(jest({ ...nowhere, KIT_ANCHOR_REQUIRED: '1' }).status).toBe(1);
    } finally {
      fs.writeFileSync(file, original);
      fs.writeFileSync(manifest, originalManifest);
    }
  }, NPM_TIMEOUT);

  it('its kit-integrity test fails on an edit to the vendored kit, naming the file and the fix', () => {
    const file = path.join(site, 'vendor/affiliate-kit/server/src/offer.ts');
    const original = fs.readFileSync(file);
    try {
      fs.appendFileSync(file, '\n// a local "fix"\n');
      const r = run('node', [path.join(site, 'node_modules/jest/bin/jest.js'), '--ci', '--colors=false', 'tests/unit/kit-integrity.test.ts'], { cwd: site, env: ANCHOR_ENV_CLEARED, timeoutMs: NPM_TIMEOUT });
      expect(r.status).toBe(1);
      expect(r.stderr).toContain('modified: server/src/offer.ts');
      expect(r.stderr).toContain('install/sync-site-kit.sh');
    } finally {
      fs.writeFileSync(file, original);
    }
  }, NPM_TIMEOUT);

  it('its kit-integrity test fails when Dockerfile.azure is not what deploy.sh would build: a body edit, or an image key changed in deploy.env', () => {
    // Both keep Dockerfile.azure's first line (the kit version deploy-config.test.ts pins),
    // so only the render check sees them; deploy.sh's own check would refuse the build.
    const dockerfile = path.join(site, 'Dockerfile.azure');
    const deployEnv = path.join(site, 'deploy.env');
    const originalDockerfile = fs.readFileSync(dockerfile, 'utf8');
    const originalEnv = fs.readFileSync(deployEnv, 'utf8');
    const jest = () => run('node', [path.join(site, 'node_modules/jest/bin/jest.js'), '--ci', '--colors=false', 'tests/unit/kit-integrity.test.ts',
      '-t', 'Dockerfile.azure is the vendored template'], { cwd: site, env: ANCHOR_ENV_CLEARED, timeoutMs: NPM_TIMEOUT });
    try {
      expect(describeRun(jest())).toMatch(/^exit 0/);
      const asRoot = originalDockerfile.replace(/^USER node$/m, 'USER root');
      expect(asRoot).not.toBe(originalDockerfile);
      fs.writeFileSync(dockerfile, asRoot);
      const edited = jest();
      expect(edited.status).toBe(1);
      expect(edited.stderr).toContain('Dockerfile.azure is not the vendored kit template rendered with deploy.env');
      expect(edited.stderr).toContain('render-dockerfile.sh --env deploy.env --write');
      fs.writeFileSync(dockerfile, originalDockerfile);
      const node24 = originalEnv.replace(/^NODE_IMAGE=.*$/m, 'NODE_IMAGE=node:24-alpine');
      expect(node24).not.toBe(originalEnv);
      fs.writeFileSync(deployEnv, node24);
      const env = jest();
      expect(env.status).toBe(1);
      expect(env.stderr).toContain('render-dockerfile.sh --env deploy.env --write');
      // An invalid deploy.env is named as such, not as a missed re-render.
      fs.writeFileSync(deployEnv, `${originalEnv}NOT_A_DEPLOY_KEY=1\n`);
      const invalid = jest();
      expect(invalid.status).toBe(1);
      expect(invalid.stderr).toContain('deploy.env is invalid');
    } finally {
      fs.writeFileSync(dockerfile, originalDockerfile);
      fs.writeFileSync(deployEnv, originalEnv);
    }
  }, NPM_TIMEOUT);

  it('its deploy-config test fails when the kit is synced but the Dockerfile is not re-rendered', () => {
    const file = path.join(site, 'Dockerfile.azure');
    const original = fs.readFileSync(file, 'utf8');
    try {
      fs.writeFileSync(file, original.replace(/\(kit [^)]+\)/, '(kit 0.0.1)'));
      const r = run('node', [path.join(site, 'node_modules/jest/bin/jest.js'), '--ci', '--colors=false', 'tests/unit/deploy-config.test.ts'], { cwd: site, timeoutMs: NPM_TIMEOUT });
      expect(r.status).toBe(1);
      expect(r.stderr).toContain('was rendered from the vendored kit template');
    } finally {
      fs.writeFileSync(file, original);
    }
  }, NPM_TIMEOUT);
});
