import * as fs from 'fs';
import * as path from 'path';
import {
  ANCHOR_ENV_CLEARED, CONTENT_CHOICE, CREATE, KIT, KIT_NODE_MODULES, KIT_VERSION, REPO, SCAFFOLD, SYNC, TEST_WAIVER, cleanupTmp, createSite, describeRun,
  fakeDockerBin, fakeKit, filesUnder, pythonHasYaml, readJson, registryGateProjects, run, shellcheckBin, tmpDir,
} from './helpers';

/**
 * install/create-affiliate-site.sh: what a generated site holds, that every
 * generated file passes the checks its consumer applies (the kit's site.json
 * loader, the kit's gate-config validator, the deploy scripts' deploy.env
 * validation and Dockerfile drift check, the implementer's scope policy and
 * the deploy-repair protections, git), and that a refusal or a failure
 * leaves nothing behind.
 */

const BRAND = "Joe's Gear & Co";

describe('create-affiliate-site.sh generates a site', () => {
  let tmp = '';
  let site = '';
  beforeAll(() => {
    tmp = tmpDir('create');
    const { dir, result } = createSite(tmp, {
      slug: 'gearpicks',
      extra: ['--brand', BRAND, '--niche', 'gear', '--gate-port', '55301', '--fixture-db-port', '55302', '--content-rule-sets', 'electrical,medical'],
    });
    if (result.status !== 0) throw new Error(describeRun(result));
    site = dir;
  }, 120_000);
  afterAll(() => cleanupTmp(tmp));

  it('holds every file the scaffold contract names', () => {
    const files = filesUnder(site, ['.git', 'vendor']);
    for (const f of [
      '.gitignore', '.dockerignore', 'CLAUDE.md', 'README.md', 'site.json', 'package.json', 'package-lock.json', 'tsconfig.json',
      'tsconfig.build.json', 'tsconfig.jest.json', 'jest.config.js', 'Dockerfile.azure', 'deploy.env', 'docker-compose.yml',
      'docker-compose.test.yml', 'keep-the-lights-on.yaml', 'src/index.ts', 'src/routes/table.ts', 'src/render/pages.ts',
      'src/render/sitemaps.ts', 'src/db/pool.ts', 'src/db/articles.ts', 'src/config/site.ts', 'src/gear/policy.ts',
      'db/migrations/0000_site_defaults.sql', 'db/fixtures/seed.sql', 'db/fixtures/freshen.sql', 'content/llms.json',
      'tests/suite.env', 'tests/run-local-suite.sh', 'tests/playwright.config.ts', 'tests/ui/gate.config.ts', 'tests/ui/pages.manifest.ts',
      'tests/ui/site.spec.ts', 'tests/unit/kit-integrity.test.ts', 'tests/unit/gate-config.test.ts', 'tests/unit/app.test.ts',
      'tests/unit/deploy-config.test.ts', 'agents/register-with-framework.sh', 'agents/implementer-scope.yaml', 'agents/README.md',
      'prompts/implementer/README.md', 'azure/provision.sh', 'azure/deploy.sh', 'azure/teardown.sh', 'aws/provision.sh', 'aws/deploy.sh',
      'aws/teardown.sh',
    ]) expect(files).toContain(f);
    expect(files.filter((f) => f.endsWith('.tmpl') || f.includes('__NICHE__') || /(^|\/)dot-/.test(f))).toEqual([]);
    for (const f of files.filter((x) => x.endsWith('.sh'))) expect(fs.statSync(path.join(site, f)).mode & 0o777).toBe(0o755);
    expect(fs.statSync(path.join(site, 'site.json')).mode & 0o777).toBe(0o644);
  });

  it('leaves no placeholder anywhere outside the vendored kit', () => {
    for (const f of filesUnder(site, ['.git', 'vendor'])) {
      expect(`${f}: ${/\{\{\s*(?:[a-z]+:)?[A-Za-z_]\w*\s*\}\}/.test(fs.readFileSync(path.join(site, f), 'utf8'))}`).toBe(`${f}: false`);
    }
  });

  it('site.json passes the kit\'s own loader with the scaffold\'s values', () => {
    const r = run('node', [path.join(SCAFFOLD, 'validate_site_json.cjs'), '--kit', KIT, path.join(site, 'site.json')]);
    expect(describeRun(r)).toMatch(/^exit 0/);
    const ctx = JSON.parse(r.stdout);
    expect(ctx).toMatchObject({ siteId: 'gearpicks', brandName: BRAND, canonicalOrigin: 'https://gearpicks.example', partnerTag: 'gearpicks-20' });
    const raw = readJson<{ disclosure: { earnings: string }; byline: { name: string }; indexNow: { key: string } }>(path.join(site, 'site.json'));
    expect(raw.disclosure.earnings).toBe(`As an Amazon Associate, ${BRAND} earns from qualifying purchases.`);
    expect(raw.byline.name).toBe(`${BRAND} Editorial Team`);
    expect(raw.indexNow.key).toBe('0f4c2a9e7b1d4e3a8c6b5d2f1e0a9c7b');
  });

  it('free text lands quoted for each language (SQL, YAML, JSON, TypeScript)', () => {
    expect(fs.readFileSync(path.join(site, 'db/migrations/0000_site_defaults.sql'), 'utf8'))
      .toContain("ALTER TABLE editorial_articles ALTER COLUMN author SET DEFAULT 'Joe''s Gear & Co Editorial Team';");
    expect(fs.readFileSync(path.join(site, 'keep-the-lights-on.yaml'), 'utf8')).toContain(`display_name: ${JSON.stringify(`${BRAND} (gearpicks.example)`)}`);
    expect(readJson<{ description: string }>(path.join(site, 'package.json')).description).toContain(BRAND);
    const gate = fs.readFileSync(path.join(site, 'tests/ui/gate.config.ts'), 'utf8');
    expect(gate).toContain('export const CONTENT_RULE_SETS: readonly string[] = Object.freeze(["electrical", "medical"]);');
    expect(gate).toContain('export const CONTENT_POLICY_WAIVED: string = "";');
  });

  it('is a git repository on main with no remote and no commit', () => {
    expect(run('git', ['-C', site, 'symbolic-ref', 'HEAD']).stdout.trim()).toBe('refs/heads/main');
    expect(run('git', ['-C', site, 'remote']).stdout.trim()).toBe('');
    expect(run('git', ['-C', site, 'rev-parse', '--verify', '-q', 'HEAD']).status).not.toBe(0);
  });

  it('vendors the kit with an intact manifest', () => {
    expect(fs.readFileSync(path.join(site, 'vendor/affiliate-kit/KIT_VERSION'), 'utf8')).toBe(`${KIT_VERSION}\n`);
    expect(describeRun(run('bash', [SYNC, site, '--verify']))).toMatch(/^exit 0/);
    expect(describeRun(run('bash', [SYNC, site, '--check']))).toMatch(/^exit 0/);
    expect(fs.readFileSync(path.join(site, '.dockerignore'), 'utf8')).toBe(fs.readFileSync(path.join(KIT, 'deploy/dockerignore.example'), 'utf8'));
  });

  it('pins exactly the kit\'s dependencies, with the kit\'s lockfile under a root entry for the site', () => {
    const kitPkg = readJson<{ dependencies: object; devDependencies: object }>(path.join(KIT, 'package.json'));
    const pkg = readJson<{ name: string; dependencies: object; devDependencies: object; scripts: Record<string, string> }>(path.join(site, 'package.json'));
    expect(pkg.name).toBe('gearpicks');
    expect(pkg.dependencies).toEqual(kitPkg.dependencies);
    expect(pkg.devDependencies).toEqual(kitPkg.devDependencies);
    for (const s of ['build', 'build:image', 'typecheck', 'test', 'start', 'migrate', 'gate']) expect(pkg.scripts[s]).toBeTruthy();
    const kitLock = readJson<{ lockfileVersion: number; packages: Record<string, unknown> }>(path.join(KIT, 'package-lock.json'));
    const lock = readJson<{ name: string; lockfileVersion: number; packages: Record<string, Record<string, unknown>> }>(path.join(site, 'package-lock.json'));
    expect(lock.name).toBe('gearpicks');
    expect(lock.lockfileVersion).toBe(kitLock.lockfileVersion);
    expect(lock.packages['']).toMatchObject({ name: 'gearpicks', dependencies: pkg.dependencies, devDependencies: pkg.devDependencies });
    const { '': _siteRoot, ...rest } = lock.packages;
    const { '': _kitRoot, ...kitRest } = kitLock.packages;
    expect(rest).toEqual(kitRest);
  });

  it('deploy.env passes the deploy scripts\' validation and Dockerfile.azure is exactly their render', () => {
    // A sandboxed HOME: the secrets file deploy.env names (~/.reusable-agents/secrets.env) is never read here.
    const home = tmpDir('home');
    try {
      const r = run('bash', [path.join(site, 'vendor/affiliate-kit/deploy/render-dockerfile.sh'), '--env', path.join(site, 'deploy.env'), '--check'],
        { env: { HOME: home, AI_TRAFFIC_EXCLUDE_IPS: '' } });
      expect(describeRun(r)).toMatch(/^exit 0/);
      expect(r.stdout).toContain('up to date with the kit template');
    } finally {
      cleanupTmp(home);
    }
    const env = fs.readFileSync(path.join(site, 'deploy.env'), 'utf8');
    for (const line of [
      'APP_NAME=gearpicks', 'SITE_DOMAIN=gearpicks.example', 'SITE_HOSTNAMES="gearpicks.example www.gearpicks.example"', 'BUILD_SCRIPT=build:image',
      'DB_NAME=gearpicks', 'DB_PUBLISHER_ROLE=gearpicks_publisher', 'DB_AGENTS_DSN=env:DATABASE_URL_GEARPICKS',
      'DB_PUBLISHER_GRANTS="SELECT,UPDATE(status,published_at):editorial_articles"',
      'MIGRATE_COMMAND="node dist/vendor/affiliate-kit/server/src/db/migrate.js --site-dir db/migrations"',
      // Only routes the scaffold serves (the kit example's product and best-of paths are not), and the site's own AWS secret.
      'SMOKE_PATHS="/ /health /robots.txt /llms.txt /sitemap.xml /guides /about"', 'AWS_SECRET_ID=nsc/gearpicks',
    ]) expect(env.split('\n')).toContain(line);
    expect(env.split('\n').filter((l) => !l.startsWith('#') && /examplepicks|B0EXAMPLE|example-best-list/i.test(l))).toEqual([]);
    expect(fs.readFileSync(path.join(site, 'Dockerfile.azure'), 'utf8').split('\n')[0])
      .toBe(`# rendered-from: affiliate-kit/deploy/Dockerfile.azure.tmpl (kit ${KIT_VERSION})`);
  });

  it('the gate wiring points at the vendored kit runner, with this site\'s ports and the other gates\' locks', () => {
    const suite = fs.readFileSync(path.join(site, 'tests/suite.env'), 'utf8');
    const locks = [...registryGateProjects('gearpicks'), '*'].join(' ');
    expect(locks.split(' ').length).toBeGreaterThan(1);
    for (const line of ["SUITE_PROJECT='gearpicks'", "SUITE_APP_PORT='55301'", "SUITE_DB_PORT='55302'", `SUITE_HOST_LOCKS='${locks}'`,
      "SUITE_FIXTURE_FRESHEN_SQL='db/fixtures/seed.sql db/fixtures/freshen.sql'", "SUITE_DB_NAME='gearpicks_fixture'"]) {
      expect(suite.split('\n')).toContain(line);
    }
    const shim = fs.readFileSync(path.join(site, 'tests/run-local-suite.sh'), 'utf8');
    expect(shim).toContain('kit="$here/../vendor/affiliate-kit"');
    expect(shim).toContain('exec bash "$kit/tests/run-local-suite.sh" --suite-env');
    expect(shim).toContain('--verify-anchored');
    const printed = run('bash', [path.join(site, 'tests/run-local-suite.sh'), '--print-config'], { env: ANCHOR_ENV_CLEARED });
    expect(describeRun(printed)).toMatch(/^exit 0/);
    expect(printed.stdout).toContain(`vendor/affiliate-kit: kit ${KIT_VERSION}`);
    // No reusable-agents checkout next to this throwaway site: the anchor can only warn (generatedSite.test.ts has one).
    expect(printed.stderr).toContain('vendor/affiliate-kit was NOT anchored to reusable-agents');
    expect(printed.stdout).toContain('project=gearpicks');
    expect(printed.stdout).toContain('url=http://127.0.0.1:55301');
    expect(printed.stdout).toContain(`host_locks=${locks}`);
    expect(printed.stdout).toContain('compose_files=docker-compose.yml docker-compose.test.yml');
    // The CPU cap holds from container creation, not only after the runner's docker update.
    for (const f of ['docker-compose.yml', 'docker-compose.test.yml']) {
      expect(fs.readFileSync(path.join(site, f), 'utf8')).toContain('cpus: ${SUITE_CPUS:-0}');
    }
  });

  it('the gate config loads: the kit\'s validator passes it and Playwright lists every spec', () => {
    // The site's own gate-config unit test (the kit's gateConfigProblems) and
    // Playwright's loader, on the kit's node_modules: the rule-set variant
    // (generatedSite.test.ts covers the waiver variant).
    const nm = path.join(site, 'node_modules');
    fs.symlinkSync(KIT_NODE_MODULES, nm);
    try {
      const unit = run('node', [path.join(nm, 'jest/bin/jest.js'), '--ci', '--colors=false', 'tests/unit/gate-config.test.ts'], { cwd: site, timeoutMs: 240_000 });
      expect(describeRun(unit)).toMatch(/^exit 0/);
      const list = run(path.join(nm, '.bin/playwright'), ['test', '--list'], { cwd: path.join(site, 'tests'), env: { TEST_URL: 'http://127.0.0.1:9' }, timeoutMs: 240_000 });
      expect(describeRun(list)).toMatch(/^exit 0/);
      expect(list.stdout).toMatch(/content policy/i);
      expect(list.stdout).toContain('[site]');
    } finally {
      fs.rmSync(nm, { force: true });
    }
  }, 300_000);

  it('CLAUDE.md carries the north star, the Amazon data rules, the kit boundary, the gate contract and commit-to-main', () => {
    const md = fs.readFileSync(path.join(site, 'CLAUDE.md'), 'utf8');
    for (const heading of ['## North star: verified-human Amazon clicks', '## The kit boundary (read-only)', '## Operator-owned files',
      '## Amazon data rules', '## The gate contract', '## Git: commit straight to main', '## Niche safety boundary (operator-owned)']) {
      expect(md).toContain(heading);
    }
    expect(md).toContain('outbound_clicks_verified_human');
    expect(md).toContain('src/gear/policy.ts');
    expect(md).toContain('electrical,medical');
    expect(md).toContain(`kit ${KIT_VERSION}`);
    expect(md).toContain('`tests/ui/gate.config.ts`: the gate\'s policy');
    expect(md).toContain('8 fixture SQL');
    expect(md).toContain(`SUITE_HOST_LOCKS='${[...registryGateProjects('gearpicks'), '*'].join(' ')}'`);
    expect(md).toContain('publish guard');
  });

  (pythonHasYaml() ? it : it.skip)('the YAML files parse; the KTLO config names this system and the fleet operator', () => {
    const r = run('python3', ['-c', `
import json, sys, yaml
k = yaml.safe_load(open(sys.argv[1]))
s = yaml.safe_load(open(sys.argv[2]))
print(json.dumps({"system": k["system"], "owner": k["owner_email"], "db": k["db"], "home": k["urls"]["homepage"], "repo": s["implementer"]["repo_path"], "branch": s["implementer"]["branch"]}))
`, path.join(site, 'keep-the-lights-on.yaml'), path.join(site, 'agents/implementer-scope.yaml')]);
    expect(describeRun(r)).toMatch(/^exit 0/);
    expect(JSON.parse(r.stdout)).toEqual({
      system: 'gearpicks', owner: 'mperry@northernsoftwareconsulting.com', db: { gearpicks: 'DATABASE_URL_GEARPICKS' },
      home: 'https://gearpicks.example/', repo: site, branch: 'main',
    });
  });

  (pythonHasYaml() ? it : it.skip)('the implementer scope keeps the operator-owned files out of EVERY dispatch kind, and the gate out of repairs', () => {
    // The framework's own ScopePolicy, on the generated block, for every kind it
    // lists, the kinds the implementer and dispatcher know, and an unknown one;
    // and the deploy-repair checker's own protected globs.
    const r = run('python3', ['-c', `
import json, sys, yaml
sys.path.insert(0, sys.argv[2])
sys.dont_write_bytecode = True
from framework.core.implementer_scope import ScopePolicy
from framework.core.deploy_repair import DEFAULTS, _glob_match
cfg = yaml.safe_load(open(sys.argv[1]))
kinds = sorted(set(cfg["implementer"].get("scope_by_dispatch_kind", {})) | {
    "seo", "pi", "article-author", "article-refresh", "catalog-audit", "comp-research", "news-author", "news-rewrite",
    "h2h", "h2h-commentary", "comparison_page_generation", "crash-fix", "deploy-repair", "general", "kit-change",
    "never-heard-of-it"})
out = {"verdicts": {}, "protected": {}}
for kind in [None] + kinds:
    p = ScopePolicy.from_site_config(cfg, dispatch_kind=kind)
    out["verdicts"][kind or "default"] = {f: p.is_path_allowed(f) for f in [
        "vendor/affiliate-kit/server/src/links.ts", "./vendor/x.ts", "site.json", "src/gear/policy.ts", "src/routes/table.ts",
        "agents/x/site.yaml", "tests/unit/kit-integrity.test.ts", "tests/unit/app.test.ts", "CLAUDE.md", "deploy.env",
        "tests/ui/gate.config.ts", "tests/ui/pages.manifest.ts", "tests/ui/site.spec.ts", "tests/run-local-suite.sh",
        "db/migrations/0009_catalog-audit-images.sql", "changes/r1.summary.md"]}
for f in ["tests/ui/pages.manifest.ts", "tests/run-local-suite.sh", "tests/playwright.config.ts", "docker-compose.test.yml"]:
    out["protected"][f] = _glob_match(f, DEFAULTS["protected_globs"])
print(json.dumps(out))
`, path.join(site, 'agents/implementer-scope.yaml'), REPO]);
    expect(describeRun(r)).toMatch(/^exit 0/);
    const { verdicts, protected: guarded } = JSON.parse(r.stdout) as { verdicts: Record<string, Record<string, boolean>>; protected: Record<string, boolean> };
    expect(Object.keys(verdicts).length).toBeGreaterThan(15);
    for (const [kind, v] of Object.entries(verdicts)) {
      for (const owned of ['vendor/affiliate-kit/server/src/links.ts', './vendor/x.ts', 'site.json', 'src/gear/policy.ts',
        'agents/x/site.yaml', 'tests/unit/kit-integrity.test.ts', 'CLAUDE.md', 'deploy.env', 'tests/ui/gate.config.ts', 'tests/run-local-suite.sh']) {
        expect(`${kind} may write ${owned}: ${v[owned]}`).toBe(`${kind} may write ${owned}: false`);
      }
    }
    // Code dispatches may still edit the site's own code and page manifest.
    expect(verdicts.default?.['src/routes/table.ts']).toBe(true);
    expect(verdicts.default?.['tests/ui/pages.manifest.ts']).toBe(true);
    expect(verdicts.seo?.['tests/unit/app.test.ts']).toBe(true);
    // A deploy repair fixes the code (or a wrong test), never the page manifest.
    expect(verdicts['deploy-repair']?.['src/routes/table.ts']).toBe(true);
    expect(verdicts['deploy-repair']?.['tests/ui/site.spec.ts']).toBe(true);
    expect(verdicts['deploy-repair']?.['tests/ui/pages.manifest.ts']).toBe(false);
    expect(guarded).toEqual({ 'tests/ui/pages.manifest.ts': true, 'tests/run-local-suite.sh': true, 'tests/playwright.config.ts': true, 'docker-compose.test.yml': true });
    // Row-writing dispatches may edit nothing; a catalog-audit fix is a new migration plus its summary.
    for (const dbOnly of ['article-author', 'article-refresh', 'h2h']) {
      expect(verdicts[dbOnly]?.['src/routes/table.ts']).toBe(false);
      expect(verdicts[dbOnly]?.['db/migrations/0009_catalog-audit-images.sql']).toBe(false);
    }
    expect(verdicts['catalog-audit']).toMatchObject({
      'db/migrations/0009_catalog-audit-images.sql': true, 'changes/r1.summary.md': true, 'src/routes/table.ts': false, 'tests/ui/pages.manifest.ts': false,
    });
  });

  it('the shell scripts pass bash -n (and shellcheck, when the host has it)', () => {
    const scripts = [CREATE, SYNC, ...filesUnder(site, ['.git', 'vendor', 'node_modules']).filter((f) => f.endsWith('.sh')).map((f) => path.join(site, f))];
    for (const s of scripts) expect(`${s}: ${run('bash', ['-n', s]).status}`).toBe(`${s}: 0`);
    const sc = shellcheckBin();
    if (!sc) return;
    const r = run(sc, ['-x', ...scripts]);
    expect(describeRun(r)).toMatch(/^exit 0/);
  });

  it('registers nothing and runs nothing on its own: no manifest, timer, cron or remote', () => {
    expect(filesUnder(path.join(site, 'agents'), []).filter((f) => f.endsWith('manifest.json'))).toEqual([]);
    expect(fs.readFileSync(path.join(site, 'docker-compose.yml'), 'utf8')).not.toMatch(/restart:/);
    expect(fs.readFileSync(path.join(site, 'docker-compose.test.yml'), 'utf8')).not.toMatch(/restart:/);
  });
});

describe('create-affiliate-site.sh choices', () => {
  let tmp = '';
  beforeAll(() => { tmp = tmpDir('create-choices'); });
  afterAll(() => cleanupTmp(tmp));

  it('a waived content gate renders the reason, and --host-locks names exactly the gates given; a path with .. lands where it resolves', () => {
    // <tmp>/stray/../waived resolves to <tmp>/waived: nothing named stray is created.
    const dir = path.join(tmp, 'waived');
    const r = run('bash', [CREATE, 'waivedpicks', 'waived.example', '--dir', `${tmp}/stray/../waived`,
      '--content-policy-waived', 'a deals feed with no editorial text', '--host-locks', 'other-gate, other-gate,waivedpicks *'], { timeoutMs: 120_000 });
    expect(describeRun(r)).toMatch(/^exit 0/);
    expect(fs.readdirSync(tmp).sort()).toEqual(['waived']);
    const gate = fs.readFileSync(path.join(dir, 'tests/ui/gate.config.ts'), 'utf8');
    expect(gate).toContain('export const CONTENT_RULE_SETS: readonly string[] = Object.freeze([]);');
    expect(gate).toContain('export const CONTENT_POLICY_WAIVED: string = "a deals feed with no editorial text";');
    expect(fs.readFileSync(path.join(dir, 'CLAUDE.md'), 'utf8')).toContain('none: the content gate is waived (a deals feed with no editorial text)');
    // Repeated and own names dropped, '*' always last.
    expect(fs.readFileSync(path.join(dir, 'tests/suite.env'), 'utf8').split('\n')).toContain("SUITE_HOST_LOCKS='other-gate *'");
    // The canonical path, never the one with '..', is what the docs and configs carry.
    expect(fs.readFileSync(path.join(dir, 'agents/implementer-scope.yaml'), 'utf8')).toContain(`repo_path: ${dir}\n`);
    expect(fs.readFileSync(path.join(dir, 'CLAUDE.md'), 'utf8')).not.toContain('/../');
  }, 120_000);

  it('--host-locks \'\' leaves only \'*\'', () => {
    const r = run('bash', [CREATE, 'lonepicks', 'lone.example', '--dir', path.join(tmp, 'lone'), '--host-locks', '', ...CONTENT_CHOICE], { timeoutMs: 120_000 });
    expect(describeRun(r)).toMatch(/^exit 0/);
    expect(fs.readFileSync(path.join(tmp, 'lone', 'tests/suite.env'), 'utf8').split('\n')).toContain("SUITE_HOST_LOCKS='*'");
  }, 120_000);
});

describe('create-affiliate-site.sh refuses and fails cleanly', () => {
  let tmp = '';
  beforeEach(() => { tmp = tmpDir('create-refuse'); });
  afterEach(() => cleanupTmp(tmp));

  it.each([
    [['Bad_Slug', 'x.example'], 'slug'],
    [['a', 'x.example'], 'slug'],
    [['ok-slug', 'https://x.example'], 'domain'],
    [['ok-slug', 'www.x.example'], 'apex'],
    [['postgresite', 'x.example'], 'reserved'],
    [['ok-slug', 'x.example', '--partner-tag', 'NOPE'], 'partner tag'],
    [['ok-slug', 'x.example', '--gate-port', '5433', '--fixture-db-port', '5433'], 'must differ'],
    [['ok-slug', 'x.example', '--gate-port', '80'], '--gate-port'],
    [['ok-slug', 'x.example', '--content-rule-sets', 'a;b'], 'content-rule-sets'],
    [['ok-slug', 'x.example', '--brand', 'Bad "Brand"'], 'brand'],
    [['ok-slug', 'x.example', '--niche', 'routes'], 'niche'],
    [['ok-slug', 'x.example', '--frobnicate'], 'unknown option'],
    [['ok-slug'], 'expected <slug> <domain>'],
    // The content gate is never cut: no silent empty rule-set list.
    [['ok-slug', 'x.example'], 'choose the content gate'],
    [['ok-slug', 'x.example', '--content-rule-sets', 'medical', '--content-policy-waived', 'no articles at all'], 'not both'],
    [['ok-slug', 'x.example', '--content-policy-waived', 'too short'], '--content-policy-waived'],
    [['ok-slug', 'x.example', '--content-rule-sets', 'medical', '--host-locks', 'Bad!Name'], '--host-locks'],
  ])('usage error %j -> exit 2, nothing written', (args, why) => {
    const r = run('bash', [CREATE, ...(args as string[]), '--dir', path.join(tmp, 'site')]);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain(why as string);
    expect(fs.readdirSync(tmp)).toEqual([]);
  });

  it('refuses an existing non-empty directory and leaves it untouched', () => {
    const dir = path.join(tmp, 'taken');
    fs.mkdirSync(dir);
    fs.writeFileSync(path.join(dir, '.keep'), 'mine\n');
    const r = run('bash', [CREATE, 'taken', 'taken.example', '--dir', dir, ...CONTENT_CHOICE]);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('not empty');
    expect(fs.readdirSync(dir)).toEqual(['.keep']);
    expect(fs.readdirSync(tmp)).toEqual(['taken']);
  });

  it('accepts an existing empty directory', () => {
    const dir = path.join(tmp, 'empty');
    fs.mkdirSync(dir);
    const r = run('bash', [CREATE, 'emptypicks', 'empty.example', '--dir', dir, ...CONTENT_CHOICE], { timeoutMs: 120_000 });
    expect(describeRun(r)).toMatch(/^exit 0/);
    expect(fs.existsSync(path.join(dir, 'site.json'))).toBe(true);
    expect(fs.readdirSync(tmp)).toEqual(['empty']);
  }, 120_000);

  it('--dry-run checks everything and writes nothing', () => {
    const dir = path.join(tmp, 'planned');
    const r = run('bash', [CREATE, 'planpicks', 'plan.example', '--dir', dir, '--dry-run', ...CONTENT_CHOICE], { timeoutMs: 120_000 });
    expect(describeRun(r)).toMatch(/^exit 0/);
    expect(r.stdout).toContain('dry run:');
    expect(r.stdout).toContain('site.json');
    expect(r.stdout).toContain('vendor/affiliate-kit/');
    expect(r.stdout).toContain(`nothing was written to ${dir}`);
    expect(fs.existsSync(dir)).toBe(false);
    expect(fs.readdirSync(tmp)).toEqual([]);
  }, 120_000);

  it('a site that fails a step after assembly began is never moved into place, and its scratch copy is gone', () => {
    // A kit copy that is missing a migration: the template renders, then the sync refuses the kit.
    const kit = fakeKit(tmp);
    fs.rmSync(path.join(kit, 'migrations', 'kit_0001_outbound_clicks.sql'));
    fs.symlinkSync(KIT_NODE_MODULES, path.join(tmp, 'node_modules'));
    const dir = path.join(tmp, 'never');
    const r = run('bash', [CREATE, 'neverpicks', 'never.example', '--dir', dir, '--kit', kit, ...CONTENT_CHOICE]);
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('vendoring the kit failed');
    expect(fs.existsSync(dir)).toBe(false);
    expect(fs.readdirSync(tmp).sort()).toEqual([path.basename(kit), 'node_modules']);
  });

  it('says how to install the kit\'s dependencies when they are missing (exit 3)', () => {
    const kit = fakeKit(tmp);
    const r = run('bash', [CREATE, 'nodeps', 'nodeps.example', '--dir', path.join(tmp, 'nodeps'), '--kit', kit, ...CONTENT_CHOICE]);
    expect(r.status).toBe(3);
    expect(r.stderr).toContain('npm ci');
    expect(fs.existsSync(path.join(tmp, 'nodeps'))).toBe(false);
  });

  it('refuses a directory inside the reusable-agents checkout, however the path is spelled', () => {
    let r = run('bash', [CREATE, 'insidepicks', 'inside.example', '--dir', path.join(REPO, 'insidepicks'), ...CONTENT_CHOICE]);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('inside the reusable-agents checkout');
    expect(fs.existsSync(path.join(REPO, 'insidepicks'))).toBe(false);
    // Through a '..' that only resolves into the checkout (a dry run: even a wrong answer writes nothing).
    const sneaky = `${path.dirname(REPO)}/k7-no-such-dir/../${path.basename(REPO)}/insidepicks`;
    r = run('bash', [CREATE, 'insidepicks', 'inside.example', '--dir', sneaky, '--dry-run', ...CONTENT_CHOICE]);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain(`${path.join(REPO, 'insidepicks')} is inside the reusable-agents checkout`);
    expect(fs.existsSync(path.join(path.dirname(REPO), 'k7-no-such-dir'))).toBe(false);
    // And through a symbolic link to it.
    const link = path.join(tmp, 'link-to-checkout');
    fs.symlinkSync(REPO, link);
    try {
      r = run('bash', [CREATE, 'insidepicks', 'inside.example', '--dir', path.join(link, 'insidepicks'), ...CONTENT_CHOICE]);
      expect(r.status).toBe(2);
      expect(r.stderr).toContain('inside the reusable-agents checkout');
      expect(fs.existsSync(path.join(REPO, 'insidepicks'))).toBe(false);
    } finally {
      fs.unlinkSync(link);
    }
  });

  it('refuses the slug of a site the registry already has (its gate, containers, lock and database carry it)', () => {
    const [taken] = registryGateProjects('');
    expect(taken).toBeTruthy();
    const r = run('bash', [CREATE, taken as string, `${taken}-new.example`, '--dir', path.join(tmp, 'twin'), ...CONTENT_CHOICE]);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain(`slug '${taken}' is the registered site`);
    expect(fs.readdirSync(tmp)).toEqual([]);
  });

  it('refuses a slug whose gate containers or fixture volume already exist on this host', () => {
    for (const [containers, volumes, why] of [
      [['other_postgres', 'dockpicks_postgres'], [], 'a docker container named dockpicks_postgres'],
      [['dockpicks_backend_test'], [], 'a docker container named dockpicks_backend_test'],
      [[], ['dockpicks_pgdata'], 'a docker volume named dockpicks_pgdata'],
    ] as Array<[string[], string[], string]>) {
      const bin = fakeDockerBin(tmp, containers, volumes);
      const r = run('bash', [CREATE, 'dockpicks', 'dock.example', '--dir', path.join(tmp, 'dock'), '--dry-run', ...CONTENT_CHOICE],
        { env: { PATH: `${bin}:${process.env.PATH ?? ''}` } });
      expect(r.status).toBe(2);
      expect(r.stderr).toContain(why);
      fs.rmSync(bin, { recursive: true, force: true });
    }
    // Other names do not stop it.
    const bin = fakeDockerBin(tmp, ['dockpicks2_postgres', 'xdockpicks_postgres'], ['dockpicks_pgdata2']);
    const ok = run('bash', [CREATE, 'dockpicks', 'dock.example', '--dir', path.join(tmp, 'dock'), '--dry-run', ...CONTENT_CHOICE],
      { env: { PATH: `${bin}:${process.env.PATH ?? ''}` }, timeoutMs: 120_000 });
    expect(describeRun(ok)).toMatch(/^exit 0/);
  }, 120_000);

  it('the test waiver used elsewhere is a valid reason', () => {
    expect(TEST_WAIVER.length).toBeGreaterThanOrEqual(10);
  });
});
